"""ctypes bindings for the experimental OpenCL driver used by Mycelia."""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple


LOGGER = logging.getLogger(__name__)


class DriverError(RuntimeError):
    """Raised when the OpenCL driver signals an error condition."""


class HPIOAgent(ctypes.Structure):
    """Structure mirroring the native agent payload used by the driver."""

    _fields_ = [
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
        ("energy", ctypes.c_float),
        ("coupling", ctypes.c_float),
    ]


class PauliZTerm(ctypes.Structure):
    """Mirror of the driver's Pauli-Z Hamiltonian term structure."""

    _fields_ = [
        ("z_mask", ctypes.c_uint64),
        ("coefficient", ctypes.c_float),
    ]


class OpenCLDriver:
    """Thin convenience wrapper around the custom OpenCL GPU driver.

    The driver exposes a very large API surface.  Only a curated subset is
    bound here, focusing on the functionality required by the Mycelia
    architecture.  All functions are lazily resolved to keep startup time low
    and to allow unit testing without the native library.
    """

    _ERROR_CODE = ctypes.c_int

    def __init__(self, library_path: Path) -> None:
        self._library_path = library_path
        self._lib = self._load_library(library_path)
        # Many experimental builds auto-initialize GPU state.  We therefore
        # default to "ready" and only flip into a degraded mode once a call
        # explicitly reports a failure.
        self._context_ready: bool = True
        self._quantum_disabled: bool = False
        # Some driver functions return a count of affected elements where ``0``
        # simply means "no change" and should not disable the GPU context.
        self._zero_success_symbols: set[str] = {
            "step_reproduction",
            "step_pheromone_reinforce",
            "step_pheromone_diffuse_decay",
            "step_mycel_update",
            "step_colony_update",
        }

    def _load_library(self, library_path: Path) -> ctypes.CDLL:
        if not library_path.exists():
            raise FileNotFoundError(f"OpenCL driver not found at {library_path}")

        loader: type[ctypes.CDLL]
        if os.name == "nt":
            loader = ctypes.WinDLL  # type: ignore[attr-defined]
        else:
            loader = ctypes.CDLL
        return loader(str(library_path))

    def resolve(
        self,
        names: Sequence[str] | str,
        argtypes: Optional[Iterable[Any]] = None,
        restype: Any = None,
        *,
        required: bool = True,
    ) -> ctypes.CFUNCTYPE:
        """Resolve a symbol from the driver lazily.

        Multiple fallback names can be provided to support OS-specific exports.
        When ``required`` is ``False`` and none of the symbols are available, a
        no-op stub is returned to allow the Python scaffolding to run in
        environments where the experimental driver exposes only a subset of the
        API.
        """

        if isinstance(names, str):
            candidate_names: Tuple[str, ...] = (names,)
        else:
            candidate_names = tuple(names)

        for name in candidate_names:
            try:
                func = getattr(self._lib, name)
            except AttributeError:
                continue

            if argtypes is not None:
                func.argtypes = list(argtypes)
            if restype is not None:
                func.restype = restype
            return func

        if required:
            search = " / ".join(candidate_names)
            raise DriverError(f"Driver does not expose symbol '{search}'")

        LOGGER.debug("Driver does not expose symbols %s; using no-op stub", candidate_names)

        def _noop(*_args: Any) -> None:
            return None

        return _noop  # type: ignore[return-value]

    def _safe_call(
        self,
        names: Sequence[str] | str,
        *args: Any,
        argtypes: Optional[Iterable[Any]] = None,
        restype: Any = None,
        required: bool = False,
    ) -> Any:
        """Invoke a driver function while tolerating signature mismatches.

        The experimental driver evolves quickly and the Python scaffolding is
        expected to remain resilient when symbols are absent or their
        signatures differ between builds.  ``_safe_call`` resolves the symbol
        and traps ``ctypes`` conversion errors so that callers can continue to
        operate with graceful degradation instead of crashing.
        """

        try:
            func = self.resolve(
                names, argtypes=argtypes, restype=restype, required=required
            )
        except DriverError:
            if required:
                raise
            return None

        symbol_name = (
            names
            if isinstance(names, str)
            else " / ".join(str(candidate) for candidate in names)
        )

        if not self._context_ready and not self._is_lifecycle_symbol(symbol_name):
            LOGGER.debug(
                "Skipping driver call '%s' because the GPU context is unavailable",
                symbol_name,
            )
            return None

        try:
            result = func(*args)
        except (TypeError, ctypes.ArgumentError) as exc:
            LOGGER.warning(
                "OpenCL driver call '%s' failed due to argument mismatch: %s. "
                "Arguments: %s",
                symbol_name,
                exc,
                args,
            )
            return None

        self._maybe_update_context_state(symbol_name, result)
        return result

    def _is_lifecycle_symbol(self, symbol_name: str) -> bool:
        lifecycle_symbols = {
            "initialize_gpu",
            "init_gpu",
            "initialize_context",
            "shutdown_driver",
        }
        return any(symbol in symbol_name for symbol in lifecycle_symbols)

    def _maybe_update_context_state(self, symbol_name: str, status: Any) -> None:
        normalized = self._normalize_status(status)
        if normalized is None:
            return

        if isinstance(normalized, bool):
            if not normalized:
                self._context_ready = False
                LOGGER.warning(
                    "Driver call '%s' reported failure; switching to degraded mode.",
                    symbol_name,
                )
            return

        if isinstance(normalized, (int, float)):
            if normalized == 0 and symbol_name in self._zero_success_symbols:
                return
            if normalized <= 0:
                if self._context_ready:
                    LOGGER.warning(
                        "Driver call '%s' signaled failure (status=%s); disabling GPU context.",
                        symbol_name,
                        normalized,
                    )
                self._context_ready = False
            else:
                self._context_ready = True

    def _status_success(self, status: Any, *, allow_zero: bool = False) -> bool:
        normalized = self._normalize_status(status)
        if normalized is None:
            return True
        if isinstance(normalized, bool):
            return normalized
        if isinstance(normalized, (int, float)):
            if normalized == 0:
                return allow_zero
            return normalized > 0
        return bool(normalized)

    def _normalize_status(self, status: Any) -> Any:
        if status is None:
            return None
        if isinstance(status, bool):
            return status
        if isinstance(status, (int, float)):
            return status
        if hasattr(status, "value"):
            try:
                return status.value
            except AttributeError:
                return None
        return None

    # -- Lifecycle ------------------------------------------------------

    def initialize(self, gpu_index: Optional[int] = 0) -> None:
        """Attempt to initialize the GPU context if the driver exposes it.

        Many driver builds expect the caller to provide an explicit GPU index
        (typically ``0``).  When ``gpu_index`` is ``None`` the initialization is
        attempted without arguments, otherwise the index is forwarded to the
        driver.  Thanks to ``_safe_call`` mismatched signatures degrade
        gracefully when a platform ignores the parameter entirely.
        """

        args: Tuple[Any, ...]
        if gpu_index is None:
            args = ()
        else:
            args = (ctypes.c_int(int(gpu_index)),)

        if self._quantum_disabled:
            self._safe_call("set_quantum_enabled", ctypes.c_int(0), required=False)

        status = self._safe_call(
            ("initialize_gpu", "init_gpu", "initialize_context"),
            *args,
        )

        normalized = self._normalize_status(status)
        if normalized is False or (isinstance(normalized, (int, float)) and normalized < 0):
            self._context_ready = False
            LOGGER.warning(
                "GPU context initialization failed (status=%s). Running in degraded mode.",
                normalized,
            )
        elif normalized is not None:
            self._context_ready = True

    # -- SubQG interface -------------------------------------------------

    def subqg_set_params(self, noise_level: float, threshold: float) -> None:
        self._safe_call(
            "subqg_set_params",
            ctypes.c_float(noise_level),
            ctypes.c_float(threshold),
            required=False,
        )

    def init_mycelium(
        self,
        gpu_index: int,
        tile_capacity: int,
        channel_count: int,
        neighbor_degree: int,
    ) -> bool:
        status = self._safe_call(
            "subqg_init_mycel",
            ctypes.c_int(gpu_index),
            ctypes.c_int(tile_capacity),
            ctypes.c_int(channel_count),
            ctypes.c_int(neighbor_degree),
            required=False,
        )
        return self._status_success(status)

    def set_active_tiles(self, gpu_index: int, active_tiles: int) -> bool:
        status = self._safe_call(
            "subqg_set_active_T",
            ctypes.c_int(gpu_index),
            ctypes.c_int(active_tiles),
            required=False,
        )
        return self._status_success(status)

    def set_neighbors_sparse(self, gpu_index: int, neighbors: Sequence[int]) -> bool:
        if not neighbors:
            return True
        array_type = ctypes.c_int * len(neighbors)
        status = self._safe_call(
            "set_neighbors_sparse",
            ctypes.c_int(gpu_index),
            array_type(*neighbors),
            required=False,
        )
        return self._status_success(status)

    def set_mood_state(self, gpu_index: int, mood: Sequence[float]) -> bool:
        if not mood:
            return True
        array_type = ctypes.c_float * len(mood)
        status = self._safe_call(
            "set_mood_state",
            ctypes.c_int(gpu_index),
            array_type(*map(float, mood)),
            required=False,
        )
        return self._status_success(status)

    def set_nutrient_state(self, gpu_index: int, nutrients: Sequence[float]) -> bool:
        if not nutrients:
            return True
        array_type = ctypes.c_float * len(nutrients)
        status = self._safe_call(
            "set_nutrient_state",
            ctypes.c_int(gpu_index),
            array_type(*map(float, nutrients)),
            required=False,
        )
        return self._status_success(status)

    def set_diffusion_params(self, gpu_index: int, decay: float, diffusion: float) -> bool:
        status = self._safe_call(
            "set_diffusion_params",
            ctypes.c_int(gpu_index),
            ctypes.c_float(decay),
            ctypes.c_float(diffusion),
            required=False,
        )
        return self._status_success(status)

    def set_reproduction_params(
        self, gpu_index: int, nutrient_thr: float, activity_thr: float, sigma: float
    ) -> bool:
        status = self._safe_call(
            "subqg_set_repro_params",
            ctypes.c_int(gpu_index),
            ctypes.c_float(nutrient_thr),
            ctypes.c_float(activity_thr),
            ctypes.c_float(sigma),
            required=False,
        )
        return self._status_success(status)

    def set_nutrient_recovery(self, gpu_index: int, recovery: float) -> bool:
        status = self._safe_call(
            "subqg_set_nutrient_recovery",
            ctypes.c_int(gpu_index),
            ctypes.c_float(recovery),
            required=False,
        )
        return self._status_success(status)

    def subqg_initialize_state(
        self,
        gpu_index: int,
        cell_count: int,
        *,
        initial_energy: Optional[Sequence[float]] = None,
        initial_phase: Optional[Sequence[float]] = None,
        noise_level: float = 0.0,
        threshold: float = 0.0,
    ) -> bool:
        energy_ptr = (ctypes.c_float * len(initial_energy))(*initial_energy) if initial_energy else None
        phase_ptr = (ctypes.c_float * len(initial_phase))(*initial_phase) if initial_phase else None
        status = self._safe_call(
            "subqg_initialize_state_batched",
            ctypes.c_int(gpu_index),
            ctypes.c_int(cell_count),
            energy_ptr,
            phase_ptr,
            ctypes.c_float(noise_level),
            ctypes.c_float(threshold),
            required=False,
        )
        if status is None:
            return False
        normalized = self._normalize_status(status)
        if normalized is None:
            return True
        if isinstance(normalized, (int, float)) and normalized < 0:
            return False
        return bool(normalized)

    def subqg_simulation_step(
        self,
        gpu_index: int,
        batch_count: int,
        *,
        rng_energy: Optional[Sequence[float]] = None,
        rng_phase: Optional[Sequence[float]] = None,
        rng_spin: Optional[Sequence[float]] = None,
    ) -> bool:
        energy_ptr = (ctypes.c_float * len(rng_energy))(*rng_energy) if rng_energy else None
        phase_ptr = (ctypes.c_float * len(rng_phase))(*rng_phase) if rng_phase else None
        spin_ptr = (ctypes.c_float * len(rng_spin))(*rng_spin) if rng_spin else None
        status = self._safe_call(
            ("subqg_simulation_step_batched", "subqg_simulation_step"),
            ctypes.c_int(gpu_index),
            energy_ptr,
            phase_ptr,
            spin_ptr,
            ctypes.c_int(batch_count),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            ctypes.c_int(0),
            required=False,
        )
        if status is None:
            return False
        normalized = self._normalize_status(status)
        if normalized is None:
            return True
        if isinstance(normalized, (int, float)) and normalized < 0:
            return False
        return bool(normalized)

    def subqg_debug_read_field(self, out_buffer: Any, max_len: int) -> bool:
        status = self._safe_call(
            "subqg_debug_read_field",
            out_buffer,
            ctypes.c_int(max_len),
            required=False,
        )
        if status is None:
            return False
        normalized = self._normalize_status(status)
        if normalized is None:
            return True
        if isinstance(normalized, (int, float)) and normalized < 0:
            return False
        return bool(normalized)

    def subqg_release_state(self, gpu_index: int) -> None:
        self._safe_call("subqg_release_state", ctypes.c_int(gpu_index), required=False)

    def subqg_inject_agents(self, *args: Any) -> None:
        self._safe_call(("subqg_inject_agents",), *args)

    # -- Mycelial substrate ---------------------------------------------

    def mycel_reinforce_kernel(self, *args: Any) -> None:
        self._safe_call(("mycel_reinforce_kernel", "step_pheromone_reinforce"), *args)

    def mycel_diffuse_kernel(self, *args: Any) -> None:
        self._safe_call(("mycel_diffuse_kernel", "step_pheromone_diffuse_decay"), *args)

    def step_reproduction(self, *args: Any) -> None:
        self._safe_call(("step_reproduction",), *args)

    def set_pheromone_gains(self, gpu_index: int, gains: Sequence[float]) -> bool:
        if not gains:
            return True
        array_type = ctypes.c_float * len(gains)
        status = self._safe_call(
            "set_pheromone_gains",
            ctypes.c_int(gpu_index),
            array_type(*map(float, gains)),
            ctypes.c_int(len(gains)),
            required=False,
        )
        return self._status_success(status)

    def step_pheromone_reinforce(self, gpu_index: int, activity: Sequence[float]) -> bool:
        if not activity:
            return True
        array_type = ctypes.c_float * len(activity)
        status = self._safe_call(
            "step_pheromone_reinforce",
            ctypes.c_int(gpu_index),
            array_type(*map(float, activity)),
            required=False,
        )
        return self._status_success(status)

    def step_pheromone_diffuse(self, gpu_index: int) -> bool:
        status = self._safe_call(
            "step_pheromone_diffuse_decay",
            ctypes.c_int(gpu_index),
            required=False,
        )
        return self._status_success(status)

    def step_mycel_update(self, gpu_index: int, activity: Sequence[float]) -> bool:
        if not activity:
            return True
        array_type = ctypes.c_float * len(activity)
        status = self._safe_call(
            "step_mycel_update",
            ctypes.c_int(gpu_index),
            array_type(*map(float, activity)),
            required=False,
        )
        return self._status_success(status)

    def step_colony_update(self, gpu_index: int, iterations: int) -> bool:
        status = self._safe_call(
            "step_colony_update",
            ctypes.c_int(gpu_index),
            ctypes.c_int(iterations),
            required=False,
        )
        return self._status_success(status)

    def step_subqg_feedback(
        self, gpu_index: int, kappa_nutrient: float, mood_weights: Sequence[float]
    ) -> bool:
        array = None
        count = 0
        if mood_weights:
            array = (ctypes.c_float * len(mood_weights))(*map(float, mood_weights))
            count = len(mood_weights)
        status = self._safe_call(
            "step_subqg_feedback",
            ctypes.c_int(gpu_index),
            ctypes.c_float(kappa_nutrient),
            array if array is not None else None,
            ctypes.c_int(count),
            required=False,
        )
        return self._status_success(status, allow_zero=True)

    def step_reproduction_cycle(
        self, gpu_index: int, activity: Sequence[float], traits: Optional[Sequence[float]], trait_dim: int
    ) -> int:
        array_type = ctypes.c_float * len(activity) if activity else None
        activity_ptr = array_type(*map(float, activity)) if array_type else None
        proto_ptr = None
        if traits and trait_dim > 0:
            proto_array = ctypes.c_float * len(traits)
            proto_ptr = proto_array(*map(float, traits))
        status = self._safe_call(
            "step_reproduction",
            ctypes.c_int(gpu_index),
            activity_ptr,
            proto_ptr,
            ctypes.c_int(trait_dim),
            required=False,
        )
        normalized = self._normalize_status(status)
        if normalized is None:
            return 0
        return int(normalized)

    def read_pheromone_slice(
        self, gpu_index: int, channel: int, out_buffer: Any
    ) -> bool:
        status = self._safe_call(
            "read_pheromone_slice",
            ctypes.c_int(gpu_index),
            ctypes.c_int(channel),
            out_buffer,
            required=False,
        )
        return self._status_success(status)

    def read_nutrient(self, gpu_index: int, out_buffer: Any) -> bool:
        status = self._safe_call(
            "read_nutrient",
            ctypes.c_int(gpu_index),
            out_buffer,
            required=False,
        )
        return self._status_success(status)

    # -- Cognitive kernels ----------------------------------------------

    def qualia_resonator_kernel(self, *args: Any) -> None:
        self._safe_call(("qualia_resonator_kernel", "compute_qualia_resonance_gpu"), *args)

    def dream_state_generator_kernel(self, *args: Any) -> None:
        self._safe_call(("dream_state_generator_kernel", "generate_dream_state_gpu"), *args)

    def transformation_planner_kernel(self, *args: Any) -> None:
        self._safe_call(("transformation_planner_kernel", "plan_transformation_gpu"), *args)

    def system_narrative_kernel(self, *args: Any) -> None:
        self._safe_call(("system_narrative_kernel", "generate_system_narrative_gpu"), *args)

    def symbolic_abstraction_kernel(self, *args: Any) -> None:
        self._safe_call(("symbolic_abstraction_kernel", "abstract_to_symbolic_concepts_gpu"), *args)

    # -- Quantum kernels -------------------------------------------------

    def execute_vqe_gpu(
        self,
        gpu_index: int,
        num_qubits: int,
        ansatz_layers: int,
        parameters: Sequence[float],
        hamiltonian_terms: Sequence[Tuple[int, float] | PauliZTerm] | None = None,
        *,
        gradients: bool = False,
    ) -> Tuple[Optional[float], Optional[List[float]]]:
        """Run the VQE kernel with safe conversions.

        Returns the energy estimate and (optionally) the gradients reported by the
        driver when ``gradients`` is ``True``.
        """

        if not parameters:
            LOGGER.debug("Skipping VQE invocation because parameters were not provided.")
            return None, None

        if hamiltonian_terms is None:
            LOGGER.debug(
                "Skipping VQE invocation because Hamiltonian terms were not provided by the caller."
            )
            return None, None

        if not hamiltonian_terms:
            LOGGER.debug(
                "Skipping VQE invocation because the Hamiltonian term list is empty."
            )
            return None, None

        param_array_type = ctypes.c_float * len(parameters)
        param_array = param_array_type(*[float(value) for value in parameters])

        def _coerce_term(term: Tuple[int, float] | PauliZTerm) -> PauliZTerm:
            if isinstance(term, PauliZTerm):
                return term
            mask, coefficient = term
            return PauliZTerm(ctypes.c_uint64(int(mask)), ctypes.c_float(float(coefficient)))

        coerced_terms = [_coerce_term(term) for term in hamiltonian_terms]
        term_array_type = PauliZTerm * len(coerced_terms)
        term_array = term_array_type(*coerced_terms)

        energy_value = ctypes.c_float()
        gradient_array = None
        gradient_ptr = None
        if gradients:
            gradient_array_type = ctypes.c_float * len(parameters)
            gradient_array = gradient_array_type()
            gradient_ptr = gradient_array

        status = self._safe_call(
            "execute_vqe_gpu",
            ctypes.c_int(gpu_index),
            ctypes.c_int(num_qubits),
            ctypes.c_int(ansatz_layers),
            param_array,
            ctypes.c_int(len(parameters)),
            term_array,
            ctypes.c_int(len(coerced_terms)),
            ctypes.byref(energy_value),
            gradient_ptr,
            argtypes=[
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_float),
                ctypes.c_int,
                ctypes.POINTER(PauliZTerm),
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_float),
            ],
            restype=ctypes.c_int,
            required=True,
        )

        if not self._status_success(status):
            return None, None

        gradients_out: Optional[List[float]]
        if gradients and gradient_array is not None:
            gradients_out = [float(value) for value in gradient_array]
        else:
            gradients_out = None

        return float(energy_value.value), gradients_out

    def execute_grover_gpu(self, *args: Any) -> None:
        self._safe_call("execute_grover_gpu", *args)

    # -- Math primitives -------------------------------------------------

    def matmul(self, *args: Any) -> None:
        self._safe_call(("matmul", "execute_matmul_on_gpu"), *args)

    def gelu(self, *args: Any) -> None:
        self._safe_call(("gelu", "execute_gelu_on_gpu"), *args)

    def layernorm(self, *args: Any) -> None:
        self._safe_call(("layernorm", "execute_layernorm_on_gpu"), *args)

    # -- Utilities -------------------------------------------------------

    def inject_agents(
        self, gpu_index: int, agents: Sequence[Tuple[float, float, float, float]]
    ) -> bool:
        if not agents:
            return True
        agent_array = (HPIOAgent * len(agents))(
            *[HPIOAgent(float(x), float(y), float(energy), float(coupling)) for x, y, energy, coupling in agents]
        )
        status = self._safe_call(
            "subqg_inject_agents",
            ctypes.c_int(gpu_index),
            agent_array,
            ctypes.c_int(len(agents)),
            required=False,
        )
        return self._status_success(status)

    @property
    def library_path(self) -> Path:
        return self._library_path

    @property
    def context_ready(self) -> bool:
        return self._context_ready

    def disable_quantum(self) -> None:
        self._quantum_disabled = True
        self._safe_call("set_quantum_enabled", ctypes.c_int(0), required=False)

    @property
    def quantum_enabled(self) -> bool:
        return self._context_ready and not self._quantum_disabled

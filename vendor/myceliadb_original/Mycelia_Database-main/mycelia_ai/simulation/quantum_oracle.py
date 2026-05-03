"""High-level access to the quantum intuition kernels of the driver."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from ..core.driver import OpenCLDriver, PauliZTerm


LOGGER = logging.getLogger(__name__)


@dataclass
class QuantumProblem:
    """Descriptor for a variational quantum query."""

    num_qubits: int
    ansatz_layers: int
    parameters: Sequence[float]
    hamiltonian_terms: Sequence[Tuple[int, float] | PauliZTerm]
    compute_gradients: bool = False


class QuantumOracle:
    """Abstraction over the VQE and Grover acceleration kernels."""

    def __init__(self, driver: OpenCLDriver, config: dict[str, object]) -> None:
        self._driver = driver
        self._config = config
        self._enabled = bool(config.get("enabled", True))
        self._gpu_index = int(config.get("gpu_index", 0))

    def _quantum_available(self) -> bool:
        if not self._enabled:
            LOGGER.debug("Quantum-Kernel wurden per Konfiguration deaktiviert.")
            return False
        if not self._driver.quantum_enabled:
            LOGGER.debug("Treiber stellt keine Quantum-Funktionalität bereit.")
            return False
        return True

    def run_vqe(self, problem: QuantumProblem) -> Tuple[Optional[float], Optional[List[float]]]:
        if not self._quantum_available():
            return None, None

        energy, gradients = self._driver.execute_vqe_gpu(
            self._gpu_index,
            problem.num_qubits,
            problem.ansatz_layers,
            problem.parameters,
            problem.hamiltonian_terms,
            gradients=problem.compute_gradients,
        )
        return energy, gradients

    def run_grover(self, amplitudes: Sequence[float]) -> Sequence[float]:
        if not self._quantum_available():
            return amplitudes

        self._driver.execute_grover_gpu(
            amplitudes,
            self._config.get("grover_iterations", 1),
        )
        return amplitudes

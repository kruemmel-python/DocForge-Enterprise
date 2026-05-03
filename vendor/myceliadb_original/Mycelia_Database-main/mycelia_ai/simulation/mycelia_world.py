"""Simulation layer orchestrating SubQG and mycelial agent dynamics."""
from __future__ import annotations

import ctypes
import logging
import math
from dataclasses import dataclass
from typing import Dict, Generator, Iterable, List, Sequence, Tuple

from ..core.driver import OpenCLDriver
from ..core.gpu_tensor import GPUTensor, TensorArena


LOGGER = logging.getLogger(__name__)


@dataclass
class WorldSnapshot:
    """Immutable representation of a single world state."""

    step: int
    energy_field: GPUTensor
    pheromone_field: GPUTensor
    nutrient_field: GPUTensor


class MyceliaWorld:
    """High-level control surface for the physics and biological layers."""

    def __init__(self, driver: OpenCLDriver, config: Dict[str, object]) -> None:
        self._driver = driver
        self._arena = TensorArena()
        self._config = config
        self._gpu_index = int(self._config.get("gpu_index", 0))
        self._grid_shape: Tuple[int, int, int] = tuple(self._config["subqg"]["grid_shape"])
        self._cell_count = int(self._grid_shape[0] * self._grid_shape[1])

        # Ensure the native driver had a chance to set up its GPU context.
        self._driver.initialize(gpu_index=self._gpu_index)
        mode = str(self._config.get("mode", "auto")).lower()
        if mode not in {"auto", "gpu", "cpu"}:
            LOGGER.warning("Unbekannter Simulationsmodus '%s'. Nutze 'auto'.", mode)
            mode = "auto"
        self._subqg_initialized = False
        self._gpu_available = self._driver.context_ready and mode != "cpu"
        self._fallback_phase = 0.0
        self._fallback_state = {
            "energy": 0.5,
            "pheromone": 0.5,
            "nutrient": 0.5,
        }
        self._mycel_config = dict(self._config.get("mycelium", {}))
        self._mycel_channels = max(1, int(self._mycel_config.get("pheromone_channels", 4)))
        self._mycel_neighbor_degree = max(1, int(self._mycel_config.get("neighbor_degree", 6)))
        self._mycel_active_tiles = max(
            1, min(int(self._mycel_config.get("colony_count", 32)), self._cell_count)
        )
        self._mycel_initialized = False
        self._mycel_activity: List[float] = [0.0] * self._cell_count
        self._mycel_neighbors: List[int] | None = None
        self._mycel_feedback = self._mycel_config.get("feedback", {})
        mood_weights = list(self._mycel_feedback.get("mood_weights", []))
        if not mood_weights:
            mood_weights = [1.0 for _ in range(self._mycel_channels)]
        elif len(mood_weights) < self._mycel_channels:
            mood_weights.extend([mood_weights[-1]] * (self._mycel_channels - len(mood_weights)))
        self._mycel_feedback_mood = mood_weights[: self._mycel_channels]
        reinforce = list(self._mycel_config.get("reinforcement_gains", []))
        if not reinforce:
            reinforce = [1.0 for _ in range(self._mycel_channels)]
        elif len(reinforce) < self._mycel_channels:
            reinforce.extend([reinforce[-1]] * (self._mycel_channels - len(reinforce)))
        self._mycel_reinforce_gains = reinforce[: self._mycel_channels]
        self._current_step = 0
        self._last_reproduction_events = 0
        if not self._gpu_available:
            LOGGER.warning(
                "GPU-Kontext nicht verfügbar. Wechsle in vereinfachte CPU-Simulation."
            )

        self._initialize_fields()
        if self._gpu_available:
            if not self._initialize_gpu_state():
                self._gpu_available = False
            else:
                if not self._initialize_mycel_state():
                    LOGGER.warning(
                        "Myzel-Subsystem konnte nicht initialisiert werden. CPU-Fallback aktiv."
                    )
                    self._gpu_available = False

    def _initialize_gpu_state(self) -> bool:
        if not self._driver.context_ready:
            self._driver.initialize(gpu_index=self._gpu_index)
            if not self._driver.context_ready:
                LOGGER.warning(
                    "GPU-Kontext nicht verfügbar. Wechsle in vereinfachte CPU-Simulation."
                )
                self._subqg_initialized = False
                return False

        noise_level = float(self._config["subqg"].get("noise_level", 0.0))
        threshold = float(self._config["subqg"].get("threshold", 0.0))

        self._driver.subqg_set_params(noise_level, threshold)
        if not self._driver.subqg_initialize_state(
            self._gpu_index,
            self._cell_count,
            noise_level=noise_level,
            threshold=threshold,
        ):
            LOGGER.warning("Initialisierung des SubQG-Zustands schlug fehl. Fallback aktiv.")
            self._subqg_initialized = False
            return False

        if not self._refresh_energy_field():
            LOGGER.warning("Konnte anfängliches Energiefeld nicht lesen. Nutze Fallback.")
            self._subqg_initialized = False
            return False

        self._subqg_initialized = True
        return True

    def _build_neighbor_lattice(self) -> List[int]:
        if self._mycel_neighbors is not None:
            return self._mycel_neighbors

        width, height, _depth = self._grid_shape
        max_neighbors = self._mycel_neighbor_degree
        neighbors: List[int] = []
        offsets = [
            (-1, 0),
            (1, 0),
            (0, -1),
            (0, 1),
            (-1, -1),
            (1, 1),
            (-1, 1),
            (1, -1),
        ]

        for y in range(height):
            for x in range(width):
                entries: List[int] = []
                for dx, dy in offsets:
                    if len(entries) >= max_neighbors:
                        break
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        entries.append(ny * width + nx)
                while len(entries) < max_neighbors:
                    entries.append(-1)
                neighbors.extend(entries)

        self._mycel_neighbors = neighbors
        return neighbors

    def _derive_activity(self, energy_field: Sequence[float]) -> List[float]:
        if not energy_field:
            return [0.0] * self._cell_count
        minimum = min(energy_field)
        maximum = max(energy_field)
        span = max(maximum - minimum, 1e-6)
        return [float((value - minimum) / span) for value in energy_field]

    def _initialize_mycel_state(self) -> bool:
        if not self._driver.context_ready:
            return False

        if not self._driver.init_mycelium(
            self._gpu_index,
            self._cell_count,
            self._mycel_channels,
            self._mycel_neighbor_degree,
        ):
            LOGGER.warning("Initialisierung des Myzel-Subsystems fehlgeschlagen.")
            return False

        if not self._driver.set_active_tiles(self._gpu_index, self._mycel_active_tiles):
            LOGGER.warning("Aktivierung der Myzel-Tiles schlug fehl.")

        neighbors = self._build_neighbor_lattice()
        if not self._driver.set_neighbors_sparse(self._gpu_index, neighbors):
            LOGGER.warning("Übermittlung des Nachbarschaftsgitters fehlgeschlagen.")

        mood = []
        for idx in range(self._cell_count):
            base = (idx % self._mycel_channels) / max(1, self._mycel_channels - 1)
            mood.extend([base * (channel + 1) / (self._mycel_channels + 1) for channel in range(self._mycel_channels)])
        if mood:
            self._driver.set_mood_state(self._gpu_index, mood)

        nutrients = [self._mycel_config.get("nutrient_influx", 0.15)] * self._cell_count
        self._driver.set_nutrient_state(self._gpu_index, nutrients)

        self._driver.set_pheromone_gains(self._gpu_index, self._mycel_reinforce_gains)

        decay = float(self._mycel_config.get("pheromone_decay", 0.81))
        diffusion = float(self._mycel_config.get("diffusion_rate", max(0.05, 1.0 - decay)))
        self._driver.set_diffusion_params(self._gpu_index, decay, diffusion)

        reproduction_cfg = self._mycel_config.get("reproduction", {})
        self._driver.set_reproduction_params(
            self._gpu_index,
            float(reproduction_cfg.get("nutrient_threshold", 0.6)),
            float(reproduction_cfg.get("activity_threshold", 0.45)),
            float(reproduction_cfg.get("mutation_sigma", 0.05)),
        )

        self._driver.set_nutrient_recovery(
            self._gpu_index, float(self._mycel_config.get("nutrient_influx", 0.15))
        )

        self._driver.step_subqg_feedback(
            self._gpu_index,
            float(self._mycel_feedback.get("kappa_nutrient", 0.25)),
            self._mycel_feedback_mood,
        )

        if not self._update_biological_fields():
            LOGGER.warning("Auslesen des initialen Myzel-Zustands fehlgeschlagen.")
            return False

        LOGGER.info(
            "Myzel initialisiert: aktive Tiles=%d, Kanäle=%d, Nachbarn=%d",
            self._mycel_active_tiles,
            self._mycel_channels,
            self._mycel_neighbor_degree,
        )
        self._mycel_initialized = True
        return True

    def _update_biological_fields(self) -> bool:
        pheromone_edges = self._cell_count * self._mycel_neighbor_degree
        pheromone_buffer = (ctypes.c_float * pheromone_edges)()
        nutrient_buffer = (ctypes.c_float * self._cell_count)()

        pheromone_ok = self._driver.read_pheromone_slice(
            self._gpu_index, 0, pheromone_buffer
        )
        nutrient_ok = self._driver.read_nutrient(self._gpu_index, nutrient_buffer)

        if not (pheromone_ok and nutrient_ok):
            return False

        aggregated: List[float] = [0.0] * self._cell_count
        for idx in range(self._cell_count):
            base = idx * self._mycel_neighbor_degree
            edge_values = pheromone_buffer[base : base + self._mycel_neighbor_degree]
            aggregated[idx] = sum(edge_values) / max(1, self._mycel_neighbor_degree)

        self._arena.get("pheromone").payload = [float(value) for value in aggregated]
        self._arena.get("nutrient").payload = [
            float(nutrient_buffer[idx]) for idx in range(self._cell_count)
        ]
        return True

    def _log_gpu_biology(self) -> None:
        pheromones = self._arena.get("pheromone").payload or []
        nutrients = self._arena.get("nutrient").payload or []
        activity = self._mycel_activity or []

        def _mean(values: Sequence[float]) -> float:
            return float(sum(values) / len(values)) if values else 0.0

        LOGGER.info(
            "GPU-Myzel Schritt %d: Aktivität Ø=%.3f | Pheromon Ø=%.3f | Nährstoff Ø=%.3f | Reproduktion=%d",
            self._current_step,
            _mean(activity),
            _mean(pheromones),
            _mean(nutrients),
            self._last_reproduction_events,
        )

    def _apply_cpu_agent_injection(
        self, descriptors: Sequence[Tuple[float, float, float, float]]
    ) -> None:
        if not descriptors:
            return
        for _x, _y, energy, coupling in descriptors:
            self._fallback_state["energy"] = max(
                0.0, min(1.0, self._fallback_state["energy"] + energy * 0.05)
            )
            self._fallback_state["pheromone"] = max(
                0.0, min(1.0, self._fallback_state["pheromone"] + coupling * 0.03)
            )
            self._fallback_state["nutrient"] = max(
                0.0, min(1.0, self._fallback_state["nutrient"] + energy * 0.02)
            )
        LOGGER.info(
            "CPU-Fallback: %d Agenten beeinflussen den Zustand (E=%.2f, P=%.2f, N=%.2f)",
            len(descriptors),
            self._fallback_state["energy"],
            self._fallback_state["pheromone"],
            self._fallback_state["nutrient"],
        )
    def _initialize_fields(self) -> None:
        grid_shape = self._grid_shape

        energy_payload = [self._fallback_state["energy"]] * self._cell_count
        pheromone_payload = [self._fallback_state["pheromone"]] * self._cell_count
        nutrient_payload = [self._fallback_state["nutrient"]] * self._cell_count

        self._arena.register(
            "energy",
            GPUTensor(handle=0, shape=grid_shape, dtype="float32", payload=energy_payload),
        )
        self._arena.register(
            "pheromone",
            GPUTensor(handle=0, shape=grid_shape, dtype="float32", payload=pheromone_payload),
        )
        self._arena.register(
            "nutrient",
            GPUTensor(handle=0, shape=grid_shape, dtype="float32", payload=nutrient_payload),
        )

    def _run_physics_step(self, time_step: float) -> None:
        if not self._subqg_initialized:
            if not self._initialize_gpu_state():
                self._gpu_available = False
                return

        if not self._driver.subqg_simulation_step(
            self._gpu_index, self._cell_count
        ):
            LOGGER.warning("SubQG-Simulation meldete Fehler. Wechsel in CPU-Fallback.")
            self._subqg_initialized = False
            self._gpu_available = False
            self._mycel_initialized = False
            return
        if not self._refresh_energy_field():
            LOGGER.warning("Auslesen des Energiefeldes schlug fehl. Wechsel in CPU-Fallback.")
            self._subqg_initialized = False
            self._gpu_available = False
            self._mycel_initialized = False

    def _run_mycelial_step(self) -> None:
        if not self._gpu_available:
            return
        if not self._mycel_initialized:
            if not self._initialize_mycel_state():
                self._mycel_initialized = False
                self._gpu_available = False
                return

        energy_payload = self._arena.get("energy").payload or []
        if not isinstance(energy_payload, Sequence):
            return

        self._mycel_activity = self._derive_activity(list(energy_payload))
        if not self._mycel_activity:
            return

        reinforce_ok = self._driver.step_pheromone_reinforce(
            self._gpu_index, self._mycel_activity
        )
        diffuse_ok = self._driver.step_pheromone_diffuse(self._gpu_index)
        nutrient_ok = self._driver.step_mycel_update(
            self._gpu_index, self._mycel_activity
        )
        reproduction_events = self._driver.step_reproduction_cycle(
            self._gpu_index, self._mycel_activity, None, 0
        )
        self._last_reproduction_events = max(0, reproduction_events)
        colony_ok = self._driver.step_colony_update(self._gpu_index, 1)
        feedback_ok = self._driver.step_subqg_feedback(
            self._gpu_index,
            float(self._mycel_feedback.get("kappa_nutrient", 0.25)),
            self._mycel_feedback_mood,
        )

        if not all([reinforce_ok, diffuse_ok, nutrient_ok, colony_ok, feedback_ok]):
            LOGGER.warning(
                "Myzel-Schritt meldete Fehler. Wechsel in CPU-Fallback und CPU-Biologie."
            )
            self._mycel_initialized = False
            self._gpu_available = False
            return

        if not self._update_biological_fields():
            LOGGER.warning(
                "Aktualisierung der Myzel-Felder fehlgeschlagen. CPU-Fallback übernimmt."
            )
            self._mycel_initialized = False
            self._gpu_available = False
            return

        self._log_gpu_biology()

    def _refresh_energy_field(self) -> bool:
        field = self._arena.get("energy")
        buffer = (ctypes.c_float * self._cell_count)()
        if not self._driver.subqg_debug_read_field(buffer, self._cell_count):
            return False
        field.payload = [buffer[idx] for idx in range(self._cell_count)]
        return True

    def _simulate_fallback_step(self, time_step: float) -> None:
        self._fallback_phase += time_step
        energy = 0.5 + 0.5 * math.sin(self._fallback_phase)
        pheromone = 0.5 + 0.5 * math.sin(self._fallback_phase * 0.5 + math.pi / 4)
        nutrient = 0.5 + 0.5 * math.cos(self._fallback_phase * 0.33)

        self._fallback_state["energy"] = max(0.0, min(1.0, energy))
        self._fallback_state["pheromone"] = max(0.0, min(1.0, pheromone))
        self._fallback_state["nutrient"] = max(0.0, min(1.0, nutrient))

        energy_field = [self._fallback_state["energy"]] * self._cell_count
        pheromone_field = [self._fallback_state["pheromone"]] * self._cell_count
        nutrient_field = [self._fallback_state["nutrient"]] * self._cell_count

        self._arena.get("energy").payload = energy_field
        self._arena.get("pheromone").payload = pheromone_field
        self._arena.get("nutrient").payload = nutrient_field
        self._mycel_activity = [self._fallback_state["energy"]] * self._cell_count
        LOGGER.info(
            "CPU-Fallback Schritt %d: Energie=%.3f | Pheromon=%.3f | Nährstoff=%.3f",
            self._current_step,
            self._fallback_state["energy"],
            self._fallback_state["pheromone"],
            self._fallback_state["nutrient"],
        )
        self._last_reproduction_events = 0

    def _emit_snapshot(self, step: int) -> WorldSnapshot:
        return WorldSnapshot(
            step=step,
            energy_field=self._arena.get("energy"),
            pheromone_field=self._arena.get("pheromone"),
            nutrient_field=self._arena.get("nutrient"),
        )

    def evolve(self) -> Generator[WorldSnapshot, None, None]:
        time_step: float = float(self._config["time_step"])
        step = 0
        while True:
            self._current_step = step
            was_gpu_available = self._gpu_available and self._subqg_initialized
            currently_ready = self._driver.context_ready

            if currently_ready and not self._subqg_initialized:
                if self._initialize_gpu_state():
                    LOGGER.info(
                        "GPU-Kontext wiederhergestellt. Zurück zur Treiber-Simulation."
                    )
                    self._mycel_initialized = False

            if not currently_ready and was_gpu_available:
                LOGGER.warning(
                    "GPU-Kontext verloren. Schalte erneut auf CPU-Fallback um."
                )
                self._mycel_initialized = False

            self._gpu_available = currently_ready and self._subqg_initialized

            if self._gpu_available:
                self._run_physics_step(time_step)
                if not self._gpu_available:
                    LOGGER.warning(
                        "GPU-Schritt fehlgeschlagen. CPU-Fallback übernimmt weitere Schritte."
                    )
                else:
                    self._run_mycelial_step()
            if not self._gpu_available:
                self._simulate_fallback_step(time_step)
            yield self._emit_snapshot(step)
            step += 1

    def inject_agents(self, descriptors: Iterable[Sequence[float]]) -> None:
        agents: List[Tuple[float, float, float, float]] = []
        for entry in descriptors:
            try:
                x, y, energy, coupling = entry  # type: ignore[misc]
            except ValueError:
                try:
                    x, y, energy = entry  # type: ignore[misc]
                    coupling = 1.0
                except ValueError:
                    LOGGER.warning("Unbekanntes Agentenformat: %s", entry)
                    continue
            agents.append((float(x), float(y), float(energy), float(coupling)))

        if not agents:
            LOGGER.info("Keine Agenten zur Injektion übergeben.")
            return

        if self._gpu_available and self._driver.inject_agents(self._gpu_index, agents):
            LOGGER.info("GPU: %d Agenten in das SubQG injiziert.", len(agents))
            return

        LOGGER.warning(
            "Agenteninjektion über GPU nicht möglich. Nutze CPU-Fallback-Verarbeitung."
        )
        self._gpu_available = False
        self._mycel_initialized = False
        self._apply_cpu_agent_injection(agents)

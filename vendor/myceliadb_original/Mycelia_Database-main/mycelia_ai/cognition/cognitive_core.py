"""Implementation of the reflective cognitive loop."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, TYPE_CHECKING

from ..core.driver import OpenCLDriver
from ..simulation.mycelia_world import WorldSnapshot
from ..simulation.quantum_oracle import QuantumOracle, QuantumProblem
from .dynamic_database import (
    AssociativeAgentDescriptor,
    AttractorPattern,
    DynamicAssociativeDatabase,
)
from .observer_net import Observation, ObserverNetwork
from ..io import sql_importer

if TYPE_CHECKING:  # pragma: no cover
    from ..simulation.mycelia_world import MyceliaWorld


LOGGER = logging.getLogger(__name__)


@dataclass
class CognitiveState:
    """Internal bookkeeping state for the cognitive layer."""

    harmony: float
    tension: float
    qualia: float


class CognitiveCore:
    """Coordinates observation, introspection and action planning."""

    def __init__(
        self,
        driver: OpenCLDriver,
        config: Dict[str, object],
        quantum_config: Dict[str, object] | None = None,
    ) -> None:
        self._driver = driver
        self._config = config
        self._quantum_config = quantum_config or {}
        self._observer = ObserverNetwork(driver, config)
        self._oracle = QuantumOracle(driver, self._quantum_config)
        self._quantum_enabled_setting = bool(self._quantum_config.get("enabled", True))
        self._quantum_guard_cooldown_ms = int(self._quantum_config.get("intuition_cooldown_ms", 60000))
        self._quantum_guard_burst = max(1, int(self._quantum_config.get("intuition_burst", 1)))
        self._quantum_guard_tokens = float(self._quantum_guard_burst)
        self._quantum_guard_last_refill = time.monotonic()
        self._quantum_guard_last_fire = 0.0
        self._quantum_guard_suppressed = 0
        self._quantum_guard_fired = 0
        self._quantum_guard_last_reason = "idle"
        self._last_state = CognitiveState(harmony=0.0, tension=0.0, qualia=0.0)
        database_cfg = dict(config.get("database", {})) if isinstance(config, dict) else {}
        self._database = DynamicAssociativeDatabase(
            retention=int(database_cfg.get("retention", 64)),
            noise_gain=float(database_cfg.get("noise_gain", 0.18)),
            mood_gain=float(database_cfg.get("mood_gain", 0.42)),
            agent_gain=float(database_cfg.get("agent_gain", 0.75)),
        )
        self._memory_snapshot_interval = max(0, int(database_cfg.get("memory_snapshot_interval", 25)))
        self._memory_snapshot_size = max(1, int(database_cfg.get("memory_snapshot_size", 3)))
        self._last_observation: Observation | None = None

    def reflect(self, snapshot: WorldSnapshot) -> CognitiveState:
        observation = self._observer.encode(
            {
                "pheromone": snapshot.pheromone_field,
                "nutrient": snapshot.nutrient_field,
            }
        )
        qualia = self._invoke_qualia_resonance(observation)
        state = CognitiveState(
            harmony=observation.harmony_score,
            tension=observation.tension_score,
            qualia=qualia,
        )
        self._last_state = state
        self._last_observation = observation
        self._database.observe(
            snapshot,
            observation,
            state.harmony,
            state.tension,
            state.qualia,
        )
        self._maybe_trigger_quantum_intuition(state)
        LOGGER.info(
            "CognitiveCore.reflect: DynamicAssociativeDatabase.observe meldet %d AttractorPattern (Durchschnittsstabilität=%.3f, Noise=%.3f)",
            self._database.attractor_count,
            self._database.average_stability,
            self._database.noise_factor,
        )
        if self._memory_snapshot_interval and snapshot.step % self._memory_snapshot_interval == 0:
            memory = self._database.memory_snapshot(self._memory_snapshot_size)
            if memory:
                summary = ", ".join(
                    f"{entry['signature'][:8]}(stab={entry['stability']:.3f}, visits={entry['visits']})"
                    for entry in memory
                )
                LOGGER.info(
                    "CognitiveCore Gedächtnis-Snapshot Schritt %d: %s",
                    snapshot.step,
                    summary,
                )
        return state

    def _invoke_qualia_resonance(self, observation: Observation) -> float:
        return (observation.harmony_score - observation.tension_score) * 0.5

    def configure_quantum_guard(self, config: Mapping[str, object]) -> None:
        self._quantum_guard_cooldown_ms = int(config.get("cooldown_ms", self._quantum_guard_cooldown_ms))
        self._quantum_guard_burst = max(1, int(config.get("burst", self._quantum_guard_burst)))
        self._quantum_guard_tokens = min(float(self._quantum_guard_burst), self._quantum_guard_tokens)

    def quantum_guard_status(self) -> Dict[str, object]:
        return {
            "version": "MYCELIA_QUANTUM_TENSION_GUARD_V1",
            "cooldown_ms": self._quantum_guard_cooldown_ms,
            "burst": self._quantum_guard_burst,
            "tokens": round(self._quantum_guard_tokens, 3),
            "fired": self._quantum_guard_fired,
            "suppressed": self._quantum_guard_suppressed,
            "last_reason": self._quantum_guard_last_reason,
        }

    def _quantum_guard_allow(self, state: CognitiveState) -> bool:
        now = time.monotonic()
        cooldown_s = max(0.001, self._quantum_guard_cooldown_ms / 1000.0)
        elapsed = now - self._quantum_guard_last_refill
        if elapsed > 0:
            self._quantum_guard_tokens = min(float(self._quantum_guard_burst), self._quantum_guard_tokens + elapsed / cooldown_s)
            self._quantum_guard_last_refill = now
        if now - self._quantum_guard_last_fire < cooldown_s and self._quantum_guard_tokens < 1.0:
            self._quantum_guard_suppressed += 1
            self._quantum_guard_last_reason = "cooldown"
            LOGGER.warning("Quantum intuition suppressed by Tension Circuit Breaker: tension=%.3f cooldown_ms=%d", state.tension, self._quantum_guard_cooldown_ms)
            return False
        if self._quantum_guard_tokens < 1.0:
            self._quantum_guard_suppressed += 1
            self._quantum_guard_last_reason = "token_bucket_empty"
            return False
        self._quantum_guard_tokens -= 1.0
        self._quantum_guard_last_fire = now
        self._quantum_guard_fired += 1
        self._quantum_guard_last_reason = "allowed"
        return True

    def _maybe_trigger_quantum_intuition(self, state: CognitiveState) -> None:
        if not self._quantum_enabled_setting:
            LOGGER.debug("Quantenmodul deaktiviert. Überspringe Intuitionspfad.")
            return
        if not self._driver.quantum_enabled:
            LOGGER.debug(
                "OpenCL-Treiber meldet keine Quantum-Unterstützung. Nutzung wird übersprungen."
            )
            return
        if state.tension < self._config.get("dissonance_threshold", 0.9):
            return
        if not self._quantum_guard_allow(state):
            return

        features = [state.tension, state.harmony, max(0.0, state.qualia)]
        configured_qubits = int(self._quantum_config.get("vqe_qubits", 0))
        num_qubits = configured_qubits if configured_qubits > 0 else max(1, len(features))
        ansatz_layers = int(self._quantum_config.get("vqe_layers", 1))

        if len(features) < num_qubits:
            features.extend([0.0] * (num_qubits - len(features)))

        parameter_source = self._quantum_config.get("vqe_parameters")
        expected_params = max(1, num_qubits * ansatz_layers)
        if isinstance(parameter_source, (list, tuple)):
            parameters = [float(value) for value in parameter_source]
        else:
            parameters = []
        if len(parameters) < expected_params:
            parameters.extend([0.0] * (expected_params - len(parameters)))
        elif len(parameters) > expected_params:
            parameters = parameters[:expected_params]

        hamiltonian_terms = [
            (1 << idx, float(coeff))
            for idx, coeff in enumerate(features[:num_qubits])
        ]
        problem = QuantumProblem(
            num_qubits=num_qubits,
            ansatz_layers=ansatz_layers,
            parameters=parameters,
            hamiltonian_terms=hamiltonian_terms,
            compute_gradients=bool(self._quantum_config.get("vqe_gradients", False)),
        )
        energy, gradients = self._oracle.run_vqe(problem)
        if energy is not None:
            if gradients:
                preview = ", ".join(f"{value:.4f}" for value in gradients[:4])
                if len(gradients) > 4:
                    gradient_info = f"[{preview}, …]"
                else:
                    gradient_info = f"[{preview}]"
            else:
                gradient_info = "—"
            LOGGER.info(
                "Quantum intuition VQE abgeschlossen: Energie=%.6f, Gradienten=%s",
                energy,
                gradient_info,
            )
        LOGGER.debug(
            "Simulierter Traum-/Planungszyklus: harmony=%.3f tension=%.3f qualia=%.3f",
            state.harmony,
            state.tension,
            state.qualia,
        )

    @property
    def state(self) -> CognitiveState:
        return self._last_state

    def associative_query(
        self, cue: str, intensity: float = 1.0
    ) -> List[AssociativeAgentDescriptor]:
        """Return agent descriptors encoding an associative query perturbation."""

        agents = self._database.associative_query(cue, intensity)
        query = self._database.last_query
        if query is not None:
            patterns = query.get("patterns", [])
            pattern_summary = ", ".join(str(p)[:8] for p in patterns) or "—"
            LOGGER.info(
                "CognitiveCore.associative_query: cue='%s' Intensität=%.2f → %d Agenten (AttractorPattern=%s)",
                cue,
                intensity,
                len(agents),
                pattern_summary,
        )
        return agents

    def import_sql_table(
        self,
        sql_path: str,
        table: str,
        *,
        where: str | None = None,
        limit: int | None = None,
        stability: float = 0.9,
        mood_vector: Sequence[float] | None = None,
        chaos_key: float | None = None,
    ) -> List[AttractorPattern]:
        """Import relational rows from *sql_path* and store them as attractors."""

        sql_importer.import_sql_file(sql_path)
        rows = list(sql_importer.fetch_rows(table, where=where, limit=limit))
        patterns: List[AttractorPattern] = []
        for row in rows:
            pattern = self._database.store_sql_record(
                table,
                row,
                stability=stability,
                mood_vector=tuple(float(v) for v in mood_vector) if mood_vector else None,
                chaos_key=chaos_key,
            )
            patterns.append(pattern)
        LOGGER.info(
            "CognitiveCore.import_sql_table: Tabelle=%s Datensätze=%d",
            table,
            len(patterns),
        )
        return patterns

    def query_sql_like(
        self,
        *,
        table: str | None = None,
        filters: Mapping[str, object] | None = None,
        limit: int | None = 50,
    ) -> List[Dict[str, object]]:
        """Retrieve stored SQL payloads via deterministic field filters."""

        results = self._database.query_sql_like(table, filters, limit=limit)
        LOGGER.info(
            "CognitiveCore.query_sql_like: Tabelle=%s Filter=%s Treffer=%d",
            table or "*",
            filters or {},
            len(results),
        )
        return results

    def associative_sql_query(
        self, cue: str, *, intensity: float = 1.0, limit: int | None = 20
    ) -> List[Dict[str, object]]:
        """Return SQL-backed attractors that respond to an associative cue."""

        results = self._database.associative_sql_lookup(
            cue, intensity=intensity, limit=limit
        )
        return results

    def update_sql_record(
        self,
        signature: str,
        new_row: Mapping[str, object],
        *,
        stability: float | None = None,
        mood_vector: Sequence[float] | None = None,
    ) -> bool:
        """Update the stored payload of an external attractor."""

        mood = tuple(float(v) for v in mood_vector) if mood_vector else None
        return self._database.update_sql_record(
            signature,
            new_row,
            stability=stability,
            mood_vector=mood,
        )

    def delete_sql_record(self, signature: str) -> bool:
        """Remove an external attractor and its SQL payload."""

        return self._database.delete_sql_record(signature)

    def get_sql_record(self, signature: str) -> Dict[str, object] | None:
        """Return a specific external payload including metadata."""

        return self._database.get_sql_record(signature)

    def inject_associative_query(
        self,
        world: "MyceliaWorld",
        cue: str,
        *,
        intensity: float = 1.0,
    ) -> None:
        """Inject associative agents into the world based on the dynamic database."""

        agents = self.associative_query(cue, intensity)
        if not agents:
            LOGGER.info("Associative Datenbank hat noch keine Attraktoren zum Abfragen.")
            return
        payload = [agent.as_tuple() for agent in agents]
        LOGGER.debug(
            "Injiziere %d assoziative Agenten für den Reiz '%s' (Intensität %.2f).",
            len(payload),
            cue,
            intensity,
        )
        world.inject_agents(payload)

    @property
    def database(self) -> DynamicAssociativeDatabase:
        """Expose the dynamic database for diagnostics."""

        return self._database

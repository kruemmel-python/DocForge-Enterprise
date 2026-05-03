"""Dynamic, associative database inspired by the Project Mycelia design notes."""
from __future__ import annotations

import hashlib
import logging
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

from ..simulation.mycelia_world import WorldSnapshot
from .observer_net import Observation


LOGGER = logging.getLogger(__name__)


@dataclass
class AssociativeAgentDescriptor:
    """Lightweight descriptor for agent injections used as associative probes."""

    x: float
    y: float
    energy: float
    coupling: float

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.energy, self.coupling)


@dataclass
class AttractorPattern:
    """Stores an emergent attractor that encodes information across layers."""

    signature: str
    energy_mean: float
    pheromone_mean: float
    nutrient_mean: float
    mood_vector: Tuple[float, float, float]
    stability: float = 1.0
    visits: int = 1
    energy_hash: str = field(default_factory=str)
    source_table: str | None = None
    external_payload: Dict[str, object] | None = None

    def as_dict(self) -> Dict[str, object]:
        """Return the attractor as a serialisable dictionary for reporting."""

        return {
            "signature": self.signature,
            "energy_mean": self.energy_mean,
            "pheromone_mean": self.pheromone_mean,
            "nutrient_mean": self.nutrient_mean,
            "mood_vector": self.mood_vector,
            "stability": self.stability,
            "visits": self.visits,
            "energy_hash": self.energy_hash,
            "source_table": self.source_table,
            "has_external_payload": self.external_payload is not None,
        }

    def update(
        self,
        energy_mean: float,
        pheromone_mean: float,
        nutrient_mean: float,
        mood_vector: Tuple[float, float, float],
        stability: float,
    ) -> None:
        blend = 1.0 / (self.visits + 1)
        self.energy_mean = (1.0 - blend) * self.energy_mean + blend * energy_mean
        self.pheromone_mean = (1.0 - blend) * self.pheromone_mean + blend * pheromone_mean
        self.nutrient_mean = (1.0 - blend) * self.nutrient_mean + blend * nutrient_mean
        self.mood_vector = tuple(
            (1.0 - blend) * prev + blend * new
            for prev, new in zip(self.mood_vector, mood_vector)
        )
        self.stability = (1.0 - blend) * self.stability + blend * stability
        self.visits += 1


class DynamicAssociativeDatabase:
    """Models information as attractors in a coupled SubQG/Mycelial state space."""

    def __init__(
        self,
        *,
        retention: int = 64,
        noise_gain: float = 0.18,
        mood_gain: float = 0.42,
        agent_gain: float = 0.75,
    ) -> None:
        self._retention = max(4, retention)
        self._noise_gain = max(0.0, noise_gain)
        self._mood_gain = max(0.0, mood_gain)
        self._agent_gain = max(0.05, agent_gain)
        self._noise_factor = 0.437  # seeded from an irrational value to avoid loops
        self._attractors: Dict[str, AttractorPattern] = {}
        self._energy_hash_history: List[str] = []
        self._last_query: Dict[str, object] | None = None
        self._external_records: Dict[str, Dict[str, object]] = {}
        self._table_index: MutableMapping[str, set[str]] = defaultdict(set)

    def observe(
        self,
        snapshot: WorldSnapshot,
        observation: Observation,
        harmony: float,
        tension: float,
        qualia: float,
    ) -> None:
        """Update the attractor memory with a new coupled world/cognitive state."""

        energy_field = self._flatten(snapshot.energy_field.payload, snapshot.energy_field.shape)
        pheromone_field = self._flatten(
            snapshot.pheromone_field.payload, snapshot.pheromone_field.shape
        )
        nutrient_field = self._flatten(
            snapshot.nutrient_field.payload, snapshot.nutrient_field.shape
        )

        energy_mean = self._mean(energy_field)
        pheromone_mean = self._mean(pheromone_field)
        nutrient_mean = self._mean(nutrient_field)

        chaos_key = self._update_noise(energy_field)
        energy_hash = self._hash_series(energy_field)
        signature = self._derive_signature(
            energy_mean,
            pheromone_mean,
            nutrient_mean,
            harmony,
            tension,
            qualia,
            chaos_key,
        )

        mood_vector = (
            float(harmony),
            float(tension),
            float(qualia),
        )
        stability = self._estimate_stability(observation, energy_field)

        pattern = self._attractors.get(signature)
        created = pattern is None
        if pattern is None:
            pattern = AttractorPattern(
                signature=signature,
                energy_mean=energy_mean,
                pheromone_mean=pheromone_mean,
                nutrient_mean=nutrient_mean,
                mood_vector=mood_vector,
                stability=stability,
                energy_hash=energy_hash,
            )
            self._attractors[signature] = pattern
        else:
            pattern.update(energy_mean, pheromone_mean, nutrient_mean, mood_vector, stability)
            pattern.energy_hash = energy_hash

        self._energy_hash_history.append(energy_hash)
        if len(self._energy_hash_history) > self._retention:
            self._energy_hash_history.pop(0)

        self._enforce_retention()

        LOGGER.info(
            "DynamicAssociativeDatabase.observe: %s AttractorPattern %s (Stabilität=%.3f, Besuche=%d, Chaos=%.3f)",
            "neues" if created else "aktualisiertes",
            signature[:12],
            pattern.stability,
            pattern.visits,
            chaos_key,
        )

    def generate_signature(
        self,
        *,
        energy_mean: float,
        pheromone_mean: float,
        nutrient_mean: float,
        harmony: float,
        tension: float,
        qualia: float,
        chaos_key: float | None = None,
    ) -> str:
        """Public helper to deterministically derive an attractor signature."""

        return self._derive_signature(
            energy_mean,
            pheromone_mean,
            nutrient_mean,
            harmony,
            tension,
            qualia,
            self._noise_factor if chaos_key is None else float(chaos_key),
        )

    def store_pattern(
        self,
        *,
        signature: str,
        energy_mean: float,
        pheromone_mean: float,
        nutrient_mean: float,
        mood_vector: Tuple[float, float, float],
        stability: float = 1.0,
        visits: int | None = None,
        energy_hash: str | None = None,
        source_table: str | None = None,
        external_payload: Mapping[str, object] | None = None,
    ) -> AttractorPattern:
        """Create or overwrite an attractor pattern using manual data."""

        stability_clamped = max(0.0, min(1.0, stability))
        pattern = self._attractors.get(signature)
        if pattern is None:
            pattern = AttractorPattern(
                signature=signature,
                energy_mean=float(energy_mean),
                pheromone_mean=float(pheromone_mean),
                nutrient_mean=float(nutrient_mean),
                mood_vector=tuple(float(v) for v in mood_vector),
                stability=stability_clamped,
                energy_hash=energy_hash or "",
            )
            self._attractors[signature] = pattern
        else:
            pattern.energy_mean = float(energy_mean)
            pattern.pheromone_mean = float(pheromone_mean)
            pattern.nutrient_mean = float(nutrient_mean)
            pattern.mood_vector = tuple(float(v) for v in mood_vector)
            pattern.stability = stability_clamped
            if energy_hash is not None:
                pattern.energy_hash = energy_hash
        if source_table is not None:
            pattern.source_table = source_table
        if external_payload is not None:
            payload_copy = {key: value for key, value in external_payload.items()}
            pattern.external_payload = payload_copy
            self._register_external(signature, source_table, payload_copy)
        if visits is not None:
            pattern.visits = max(1, int(visits))
        self._enforce_retention()
        return pattern

    def get_pattern(self, signature: str) -> AttractorPattern | None:
        """Return a stored pattern or ``None`` if it does not exist."""

        return self._attractors.get(signature)

    def list_patterns(self) -> List[AttractorPattern]:
        """Return all known attractor patterns sorted by recency."""

        return sorted(
            self._attractors.values(), key=lambda pattern: pattern.visits, reverse=True
        )

    def delete_pattern(self, signature: str) -> bool:
        """Remove a stored pattern, returning ``True`` if it existed."""

        pattern = self._attractors.pop(signature, None)
        if pattern is None:
            return False
        if signature in self._external_records:
            self._external_records.pop(signature, None)
            self._deregister_external(signature, pattern.source_table)
        return True

    def clear(self) -> None:
        """Remove all known attractor patterns and reset history."""

        self._attractors.clear()
        self._energy_hash_history.clear()
        self._external_records.clear()
        self._table_index.clear()

    def associative_query(self, cue: str, intensity: float = 1.0) -> List[AssociativeAgentDescriptor]:
        """Return agent descriptors that encode an associative retrieval request."""

        ranked = self._rank_patterns(cue)
        if not ranked:
            self._last_query = {
                "cue": cue,
                "intensity": float(intensity),
                "returned": 0,
                "patterns": [],
            }
            return []
        agents: List[AssociativeAgentDescriptor] = []
        top_k = max(1, min(3, int(math.ceil(intensity * 2))))
        width, height = self._infer_grid()
        for pattern in ranked[:top_k]:
            x, y = self._project_signature(pattern.signature, width, height)
            energy = self._agent_gain * intensity * self._normalize(pattern.energy_mean)
            coupling = self._agent_gain * (pattern.stability + self._noise_factor)
            agents.append(
                AssociativeAgentDescriptor(
                    x=x,
                    y=y,
                    energy=energy,
                    coupling=coupling,
                )
            )
        self._record_last_query(cue, intensity, ranked, top_k)
        LOGGER.info(
            "DynamicAssociativeDatabase.associative_query: cue='%s' Intensität=%.2f → %d Agenten",
            cue,
            intensity,
            len(agents),
        )
        return agents

    def store_sql_record(
        self,
        table_name: str,
        row: Mapping[str, object],
        *,
        stability: float = 0.9,
        mood_vector: Tuple[float, float, float] | None = None,
        chaos_key: float | None = None,
    ) -> AttractorPattern:
        """Create an attractor that mirrors an imported SQL row."""

        if not table_name:
            raise ValueError("table_name darf nicht leer sein")
        normalized_table = table_name.strip()
        encoded_row = self._encode_row_payload(normalized_table, row)
        row_hash = hashlib.sha256(encoded_row.encode("utf-8"))
        row_hash_hex = row_hash.hexdigest()
        digest = row_hash.digest()
        chaos = self._noise_factor if chaos_key is None else float(chaos_key)
        signature_material = f"sql|{normalized_table}|{row_hash_hex}"
        signature = hashlib.sha256(f"{signature_material}|{chaos:.6f}".encode("utf-8")).hexdigest()

        energy_mean, pheromone_mean, nutrient_mean, derived_mood = self._row_to_dynamics(digest)
        if mood_vector is None:
            mood = derived_mood
        else:
            mood = tuple(float(component) for component in mood_vector)

        pattern = self.store_pattern(
            signature=signature,
            energy_mean=energy_mean,
            pheromone_mean=pheromone_mean,
            nutrient_mean=nutrient_mean,
            mood_vector=mood,
            stability=stability,
            energy_hash=row_hash_hex,
            source_table=normalized_table,
            external_payload=row,
        )
        LOGGER.info(
            "DynamicAssociativeDatabase.store_sql_record: Tabelle=%s Signature=%s Stabilität=%.3f",
            normalized_table,
            signature[:12],
            pattern.stability,
        )
        return pattern

    def update_sql_record(
        self,
        signature: str,
        new_row: Mapping[str, object],
        *,
        stability: float | None = None,
        mood_vector: Tuple[float, float, float] | None = None,
    ) -> bool:
        pattern = self._attractors.get(signature)
        if pattern is None or pattern.external_payload is None:
            return False

        table_name = pattern.source_table or ""
        encoded_row = self._encode_row_payload(table_name, new_row)
        row_hash = hashlib.sha256(encoded_row.encode("utf-8"))
        row_hash_hex = row_hash.hexdigest()
        digest = row_hash.digest()
        energy_mean, pheromone_mean, nutrient_mean, derived_mood = self._row_to_dynamics(
            digest
        )
        mood = tuple(float(v) for v in (mood_vector or derived_mood))
        next_stability = pattern.stability
        if stability is not None:
            next_stability = max(0.0, min(1.0, (pattern.stability + float(stability)) * 0.5))
        old_table = pattern.source_table
        pattern.energy_mean = energy_mean
        pattern.pheromone_mean = pheromone_mean
        pattern.nutrient_mean = nutrient_mean
        pattern.mood_vector = mood
        pattern.energy_hash = row_hash_hex
        pattern.external_payload = {key: value for key, value in new_row.items()}
        if table_name:
            pattern.source_table = table_name
        pattern.stability = next_stability
        pattern.visits += 1
        self._deregister_external(signature, old_table)
        self._register_external(signature, pattern.source_table, pattern.external_payload)
        LOGGER.info(
            "DynamicAssociativeDatabase.update_sql_record: Signature=%s Stabilität=%.3f Besuche=%d",
            signature[:12],
            pattern.stability,
            pattern.visits,
        )
        return True

    def delete_sql_record(self, signature: str) -> bool:
        pattern = self._attractors.get(signature)
        if pattern is None or signature not in self._external_records:
            return False
        deleted = self.delete_pattern(signature)
        if deleted:
            LOGGER.info(
                "DynamicAssociativeDatabase.delete_sql_record: Signature=%s entfernt",
                signature[:12],
            )
        return deleted

    def get_sql_record(self, signature: str) -> Dict[str, object] | None:
        record = self._external_records.get(signature)
        if record is None:
            return None
        pattern = self._attractors.get(signature)
        if pattern is None:
            return None
        return self._format_external_result(signature, record, pattern)

    def query_sql_like(
        self,
        table: str | None = None,
        filters: Mapping[str, object] | None = None,
        *,
        limit: int | None = None,
    ) -> List[Dict[str, object]]:
        """Return stored external records that match the provided filter."""

        if not self._external_records:
            return []
        normalized_table = table.lower() if table else None
        candidates: List[str]
        if normalized_table:
            candidates = list(self._table_index.get(normalized_table, set()))
        else:
            candidates = list(self._external_records.keys())

        def _matches_filters(payload: Mapping[str, object]) -> bool:
            if not filters:
                return True
            for key, expected in filters.items():
                if payload.get(key) != expected:
                    return False
            return True

        ordered = sorted(
            (self._attractors[sig] for sig in candidates if sig in self._attractors),
            key=lambda pattern: pattern.visits,
            reverse=True,
        )

        results: List[Dict[str, object]] = []
        remaining = None if limit is None else max(0, int(limit))
        for pattern in ordered:
            record = self._external_records.get(pattern.signature)
            if record is None:
                continue
            if not _matches_filters(record["row"]):
                continue
            results.append(self._format_external_result(pattern.signature, record, pattern))
            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    break
        return results

    def associative_sql_lookup(
        self, cue: str, intensity: float = 1.0, *, limit: int | None = None
    ) -> List[Dict[str, object]]:
        ranked = self._rank_patterns(cue)
        if not ranked:
            self._record_last_query(cue, intensity, [], 0)
            return []
        results: List[Dict[str, object]] = []
        taken = 0
        max_results = None if limit is None else max(0, int(limit))
        for pattern in ranked:
            record = self._external_records.get(pattern.signature)
            if record is None:
                continue
            results.append(self._format_external_result(pattern.signature, record, pattern))
            taken += 1
            if max_results is not None and taken >= max_results:
                break
        self._record_last_query(cue, intensity, ranked, taken)
        LOGGER.info(
            "DynamicAssociativeDatabase.associative_sql_lookup: cue='%s' → %d Treffer",
            cue,
            len(results),
        )
        return results

    def _enforce_retention(self) -> None:
        if len(self._attractors) <= self._retention:
            return
        ordered = sorted(
            self._attractors.values(), key=lambda pattern: pattern.stability * pattern.visits
        )
        while len(ordered) > self._retention:
            victim = ordered.pop(0)
            self.delete_pattern(victim.signature)

    def _mean(self, values: Sequence[float]) -> float:
        if not values:
            return 0.0
        return float(sum(values) / len(values))

    def _flatten(self, payload: Iterable[float] | None, shape: Sequence[int]) -> List[float]:
        if payload is None:
            total = 1
            for dim in shape:
                total *= max(1, dim)
            return [0.0] * total
        if isinstance(payload, list):
            return payload
        return list(payload)

    def _update_noise(self, energy: Sequence[float]) -> float:
        if not energy:
            return self._noise_factor
        mean = self._mean(energy)
        variance = self._mean([(value - mean) ** 2 for value in energy])
        r = 3.6 + min(0.39, variance * self._noise_gain * 10.0)
        self._noise_factor = (r * self._noise_factor * (1.0 - self._noise_factor)) % 1.0
        return self._noise_factor

    def _hash_series(self, values: Sequence[float]) -> str:
        hasher = hashlib.sha256()
        for value in values[:1024]:
            hasher.update(f"{value:.6f}".encode("utf-8"))
        hasher.update(f"{self._noise_factor:.6f}".encode("utf-8"))
        return hasher.hexdigest()

    def _derive_signature(
        self,
        energy_mean: float,
        pheromone_mean: float,
        nutrient_mean: float,
        harmony: float,
        tension: float,
        qualia: float,
        chaos_key: float,
    ) -> str:
        raw = (
            f"{energy_mean:.6f}|{pheromone_mean:.6f}|{nutrient_mean:.6f}|"
            f"{harmony:.6f}|{tension:.6f}|{qualia:.6f}|{chaos_key:.6f}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _estimate_stability(self, observation: Observation, energy_field: Sequence[float]) -> float:
        harmony = getattr(observation, "harmony_score", 0.0)
        tension = getattr(observation, "tension_score", 0.0)
        if not energy_field:
            return 0.0
        energy_mean = self._mean(energy_field)
        deviation = self._mean([abs(value - energy_mean) for value in energy_field])
        stability = math.exp(-deviation) * (1.0 + self._mood_gain * (harmony - tension))
        return max(0.0, min(1.0, stability))

    def _cue_vector(self, cue: str) -> Tuple[float, float, float]:
        digest = hashlib.sha256(f"{cue}|{self._noise_factor:.6f}".encode("utf-8")).digest()
        return tuple(byte / 255.0 for byte in digest[:3])

    def _pattern_similarity(
        self, pattern: AttractorPattern, cue_vector: Tuple[float, float, float]
    ) -> float:
        px, py, pz = pattern.mood_vector
        cx, cy, cz = cue_vector
        mood_similarity = 1.0 - min(1.0, math.sqrt((px - cx) ** 2 + (py - cy) ** 2 + (pz - cz) ** 2))
        entropy_similarity = 1.0 - abs(pattern.stability - self._noise_factor)
        return 0.7 * mood_similarity + 0.3 * entropy_similarity

    def _infer_grid(self) -> Tuple[int, int]:
        if not self._energy_hash_history:
            return (1, 1)
        sample = next(iter(self._attractors.values()))
        seed = int(sample.energy_hash[:8], 16)
        random.seed(seed)
        width = random.randint(8, 64)
        height = random.randint(8, 64)
        return (width, height)

    def _project_signature(self, signature: str, width: int, height: int) -> Tuple[float, float]:
        seed = int(signature[:12], 16)
        random.seed(seed)
        x = random.randint(0, max(1, width - 1)) / max(1, width - 1)
        y = random.randint(0, max(1, height - 1)) / max(1, height - 1)
        return (x, y)

    def _rank_patterns(self, cue: str) -> List[AttractorPattern]:
        if not self._attractors:
            return []
        cue_vector = self._cue_vector(cue)
        return sorted(
            self._attractors.values(),
            key=lambda pattern: self._pattern_similarity(pattern, cue_vector),
            reverse=True,
        )

    def _record_last_query(
        self,
        cue: str,
        intensity: float,
        ranked: Sequence[AttractorPattern],
        top_k: int,
    ) -> None:
        self._last_query = {
            "cue": cue,
            "intensity": float(intensity),
            "returned": top_k,
            "patterns": [pattern.signature for pattern in ranked[:top_k]],
        }

    def _normalize(self, value: float) -> float:
        return max(0.0, min(1.0, (value + 1.0) * 0.5))

    def _encode_row_payload(
        self, table_name: str, row: Mapping[str, object]
    ) -> str:
        items = sorted(row.items(), key=lambda item: item[0])
        encoded = ";".join(f"{key}={value!r}" for key, value in items)
        return f"{table_name}|{encoded}"

    def _row_to_dynamics(
        self, digest: bytes
    ) -> Tuple[float, float, float, Tuple[float, float, float]]:
        def _component(offset: int, signed: bool = True) -> float:
            span = digest[offset : offset + 2]
            value = int.from_bytes(span, "big") / 65535.0
            if signed:
                return value * 2.0 - 1.0
            return value

        energy_mean = _component(0)
        pheromone_mean = _component(4)
        nutrient_mean = _component(8)
        mood_vector = (
            _component(12, signed=False),
            _component(16, signed=False),
            _component(20, signed=False),
        )
        return energy_mean, pheromone_mean, nutrient_mean, mood_vector

    def _register_external(
        self, signature: str, table_name: str | None, payload: Mapping[str, object]
    ) -> None:
        previous = self._external_records.get(signature)
        previous_table = None
        if previous is not None:
            previous_table = previous.get("table")
        normalized_table = (table_name or "").lower()
        self._external_records[signature] = {
            "table": normalized_table,
            "row": {key: value for key, value in payload.items()},
        }
        if previous_table and previous_table != normalized_table:
            bucket = self._table_index.get(previous_table)
            if bucket is not None:
                bucket.discard(signature)
                if not bucket:
                    self._table_index.pop(previous_table, None)
        if table_name:
            key = table_name.lower()
            bucket = self._table_index[key]
            bucket.add(signature)

    def _deregister_external(self, signature: str, table_name: str | None) -> None:
        if table_name:
            key = table_name.lower()
            bucket = self._table_index.get(key)
            if bucket is not None:
                bucket.discard(signature)
                if not bucket:
                    self._table_index.pop(key, None)

    def _format_external_result(
        self,
        signature: str,
        record: Mapping[str, object],
        pattern: AttractorPattern,
    ) -> Dict[str, object]:
        return {
            "signature": signature,
            "table": pattern.source_table or record.get("table"),
            "data": dict(record.get("row", {})),
            "stability": pattern.stability,
            "visits": pattern.visits,
            "mood_vector": pattern.mood_vector,
        }

    @property
    def attractor_count(self) -> int:
        return len(self._attractors)

    @property
    def noise_factor(self) -> float:
        return self._noise_factor

    @property
    def average_stability(self) -> float:
        if not self._attractors:
            return 0.0
        return mean(pattern.stability for pattern in self._attractors.values())

    @property
    def last_query(self) -> Dict[str, object] | None:
        return self._last_query

    def memory_snapshot(self, top_n: int = 3) -> List[Dict[str, object]]:
        patterns = self.list_patterns()[: max(1, top_n)]
        return [pattern.as_dict() for pattern in patterns]

    def list_external_tables(self) -> List[str]:
        """Return all table labels that have SQL payloads attached."""

        if not self._table_index:
            return []
        return sorted(self._table_index.keys())

    @property
    def external_record_count(self) -> int:
        """Expose how many attractors carry SQL-backed payloads."""

        return len(self._external_records)

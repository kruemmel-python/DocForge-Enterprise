from __future__ import annotations

import base64
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import mycelia_memory_probe as probe  # noqa: E402


class MemoryProbeVectorFragmentsTest(unittest.TestCase):
    def _vector(self, n: int = 768) -> list[float]:
        # Deterministic, non-trivial values with enough entropy for fragments.
        return [((i * 37) % 997 - 498) / 997.0 for i in range(n)]

    def test_vector_fragment_probes_cover_f32_f64_and_base64_without_raw_values(self) -> None:
        vector = self._vector()
        probes = probe._vector_fragment_probes(
            [probe.VectorSource("query:test", vector)],
            fragment_floats=16,
            max_fragments_per_vector=4,
            include_f32=True,
            include_f64=True,
            include_b64=True,
            include_ascii=False,
        )
        encodings = {p.encoding for p in probes}
        self.assertIn("float32-le-fragment", encodings)
        self.assertIn("float64-le-fragment", encodings)
        self.assertIn("float32-base64-fragment", encodings)
        self.assertTrue(all(p.kind == "embedding_query_fragment" for p in probes))
        self.assertTrue(all(len(p.raw) >= 48 for p in probes if p.encoding != "float64-le-fragment"))

    def test_scan_buffer_detects_vector_fragment(self) -> None:
        vector = self._vector()
        probes = probe._vector_fragment_probes(
            [probe.VectorSource("stored:test", vector)],
            fragment_floats=8,
            max_fragments_per_vector=2,
            include_f32=True,
            include_f64=False,
            include_b64=False,
            include_ascii=False,
        )
        self.assertGreaterEqual(len(probes), 1)
        haystack = b"prefix" + probes[0].raw + b"suffix"
        hits = probe._scan_buffer(haystack, probes)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["probe_kind"], "embedding_stored_fragment")
        self.assertTrue(hits[0]["strict_relevant"])

    def test_adapter_vault_vector_loader_uses_latest_active_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".smql_adapter" / "demo"
            root.mkdir(parents=True)
            v1 = self._vector()
            v2 = [x * 0.5 for x in self._vector()]
            raw1 = struct.pack("<" + "f" * len(v1), *v1)
            raw2 = struct.pack("<" + "f" * len(v2), *v2)
            (root / "vectors.f32").write_bytes(raw1 + raw2)
            index = [
                {
                    "id": "README-000000",
                    "collection": "demo",
                    "offset": 0,
                    "dimension": len(v1),
                    "norm": 1.0,
                    "vector_sha256": "old",
                    "payload_sha256": "p",
                    "created_at": 1.0,
                },
                {
                    "op": "delete",
                    "id": "README-000000",
                },
                {
                    "id": "README-000000",
                    "collection": "demo",
                    "offset": len(raw1),
                    "dimension": len(v2),
                    "norm": 1.0,
                    "vector_sha256": "new",
                    "payload_sha256": "p",
                    "created_at": 2.0,
                },
            ]
            (root / "index.jsonl").write_text("\n".join(json.dumps(x) for x in index), encoding="utf-8")
            loaded = probe._load_adapter_vault_vectors(
                vault=str(Path(tmp) / ".smql_adapter"),
                collection="demo",
                ids=["README-000000"],
                max_vectors=1,
            )
            self.assertEqual(len(loaded), 1)
            self.assertEqual(len(loaded[0].vector), 768)
            self.assertAlmostEqual(loaded[0].vector[10], v2[10], places=6)

    def test_vector_search_verdict_fields_are_stable_for_manual_report_logic(self) -> None:
        vector = self._vector()
        probes = probe._vector_fragment_probes(
            [probe.VectorSource("query:test", vector)],
            fragment_floats=16,
            max_fragments_per_vector=3,
        )
        manifest = [
            {
                "probe_sha256": p.hash,
                "source_probe_sha256": p.source_hash,
                "probe_kind": p.kind,
                "strict_relevant": p.kind in probe.SENSITIVE_KINDS,
                "encoding": p.encoding,
                "encoding_bytes": len(p.raw),
                "probe_label": p.label,
            }
            for p in probes
        ]
        self.assertTrue(all(row["strict_relevant"] for row in manifest))
        self.assertTrue({row["encoding"] for row in manifest}.issuperset({"float32-le-fragment", "float32-base64-fragment"}))


if __name__ == "__main__":
    unittest.main()

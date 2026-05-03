#!/usr/bin/env python3
r"""External CPU-RAM probe for MyceliaDB VRAM-residency audits.

v4 adds vector-search evidence.  It can scan a MyceliaDB process while a SMQL
vector search is executed and look for high-entropy fragments of 768D embedding
vectors in several host-memory representations:

- little-endian float32 fragments
- little-endian float64 fragments
- base64 fragments of the float32 slab used by the Python/HTTP compatibility path
- optional ASCII decimal windows

The report never prints vector values.  It records hashes, fragment labels and
hit counts only.

Important boundary:
    A negative external RAM probe is evidence, not an absolute proof.  It becomes
    a strict no-CPU-RAM claim only when combined with a sealed native ABI that
    provides its own residency/zeroization attestation.  The probe is designed to
    fail closed when it cannot scan memory, when required canaries are missing, or
    when any strict-relevant vector fragment is observed.

Windows requires PROCESS_QUERY_INFORMATION and PROCESS_VM_READ rights.  Linux
requires permission to read /proc/<pid>/mem and often ptrace_scope adjustment or
root privileges.

Examples:
    # Legacy cleartext probe:
    python tools/mycelia_memory_probe.py --pid 1234 --probe "Krümmel" --json-out probe.json

    # v1.22c vector-search probe using adapter vault vectors and a live query command:
    python tools/mycelia_memory_probe.py ^
      --pid 1234 ^
      --during-smql-vector-search ^
      --adapter-vault C:\web_sicherheit\SMQL-Embedding-Adapter\.smql_adapter ^
      --collection demo ^
      --vector-id README-000002 ^
      --during-command "python -m smql_embedding_adapter.cli --mycelia-url http://127.0.0.1:9999 --mycelia-token-file C:\web_sicherheit\html\keys\local_transport.token --lmstudio-url http://127.0.0.1:1234 --embedding-model text-embedding-nomic-embed-text-v2-moe --collection demo --search-backend mycelia smql \"FIND ASSOCIATED WITH TEXT 'Was ist SMQL?' LIMIT 3\"" ^
      --scan-duration-seconds 8 ^
      --json-out vector_probe.json
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import math
import os
import platform
import re
import shlex
import struct
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCANNER_VERSION = "MYCELIA_CPU_RAM_PROBE_V4_VECTOR_SEARCH"


@dataclass
class Region:
    start: int
    end: int
    readable: bool
    label: str = ""

    @property
    def size(self) -> int:
        return max(0, self.end - self.start)


SENSITIVE_KINDS = {
    "sensitive_cleartext",
    "profile_cleartext",
    "content_body",
    "credential_equivalent",
    "embedding_vector_fragment",
    "embedding_query_fragment",
    "embedding_stored_fragment",
}
CANARY_KINDS = {"probe_canary_positive"}
NON_STRICT_KINDS = {"public_identifier", "audit_artifact"}


def _auto_probe_kind(value: str) -> str:
    """Classify probes without leaking the plaintext value into the report.

    64-hex strings are operational node handles/signatures in MyceliaDB.  They
    are still reported, but strict residency certification does not treat them
    like user-provided cleartext.  Everything else defaults to sensitive.
    """
    if re.fullmatch(r"[0-9a-fA-F]{64}", value or ""):
        return "public_identifier"
    if re.fullmatch(r"[0-9a-fA-F]{32}", value or ""):
        return "audit_artifact"
    return "sensitive_cleartext"


@dataclass(frozen=True)
class ProbeSpec:
    value: str
    kind: str


@dataclass(frozen=True)
class EncodedProbe:
    raw: bytes
    kind: str
    source_hash: str
    encoding: str
    label: str = ""

    @property
    def hash(self) -> str:
        return _hash_probe(self.raw)


def _probe_bytes(specs: list[ProbeSpec]) -> list[EncodedProbe]:
    out: list[EncodedProbe] = []
    for spec in specs:
        if not spec.value:
            continue
        source_hash = hashlib.sha256(spec.value.encode("utf-8")).hexdigest()
        out.append(EncodedProbe(spec.value.encode("utf-8"), spec.kind, source_hash, "utf-8"))
        # Many Python/Windows allocations may use UTF-16LE internally.
        out.append(EncodedProbe(spec.value.encode("utf-16le"), spec.kind, source_hash, "utf-16le"))
    return _dedupe_probes(out)


def _dedupe_probes(probes: list[EncodedProbe]) -> list[EncodedProbe]:
    unique: list[EncodedProbe] = []
    seen: set[tuple[bytes, str]] = set()
    for item in probes:
        key = (item.raw, item.kind)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _load_probe_file(path: str | None) -> list[str]:
    if not path:
        return []
    p = Path(path)
    return [line.rstrip("\n") for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hash_probe(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _scan_buffer(buf: bytes, probes: list[EncodedProbe]) -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    for probe in probes:
        idx = buf.find(probe.raw)
        if idx >= 0:
            strict_relevant = probe.kind in SENSITIVE_KINDS
            hits.append({
                "probe_sha256": probe.hash,
                "source_probe_sha256": probe.source_hash,
                "probe_kind": probe.kind,
                "strict_relevant": strict_relevant,
                "encoding": probe.encoding,
                "encoding_bytes": len(probe.raw),
                "probe_label": probe.label,
                "offset": idx,
            })
    return hits


def _linux_regions(pid: int) -> Iterable[Region]:
    maps_path = Path(f"/proc/{pid}/maps")
    for line in maps_path.read_text(errors="ignore").splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) < 2:
            continue
        addr, perms = parts[0], parts[1]
        label = parts[5] if len(parts) >= 6 else ""
        if "r" not in perms:
            continue
        start_s, end_s = addr.split("-", 1)
        yield Region(int(start_s, 16), int(end_s, 16), True, label)


def _scan_linux(pid: int, probes: list[EncodedProbe], max_region_bytes: int) -> tuple[list[dict[str, object]], int, int]:
    findings: list[dict[str, object]] = []
    scanned_regions = 0
    scanned_bytes = 0
    mem_path = Path(f"/proc/{pid}/mem")
    with mem_path.open("rb", buffering=0) as mem:
        for region in _linux_regions(pid):
            if region.size <= 0:
                continue
            to_read = min(region.size, max_region_bytes)
            try:
                mem.seek(region.start)
                data = mem.read(to_read)
            except Exception:
                continue
            scanned_regions += 1
            scanned_bytes += len(data)
            for hit in _scan_buffer(data, probes):
                findings.append({
                    "region_start": hex(region.start),
                    "region_end": hex(region.end),
                    "region_label": region.label,
                    **hit,
                })
    return findings, scanned_regions, scanned_bytes


# Windows structures based on MEMORY_BASIC_INFORMATION.
class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]


def _scan_windows(pid: int, probes: list[EncodedProbe], max_region_bytes: int) -> tuple[list[dict[str, object]], int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    MEM_COMMIT = 0x1000
    PAGE_NOACCESS = 0x01
    PAGE_GUARD = 0x100

    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise PermissionError(f"OpenProcess failed for pid={pid}, error={ctypes.get_last_error()}")

    findings: list[dict[str, object]] = []
    scanned_regions = 0
    scanned_bytes = 0
    address = 0
    mbi = MEMORY_BASIC_INFORMATION()
    try:
        while kernel32.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            base = int(mbi.BaseAddress or 0)
            size = int(mbi.RegionSize or 0)
            protect = int(mbi.Protect or 0)
            state = int(mbi.State or 0)
            readable = state == MEM_COMMIT and not (protect & PAGE_NOACCESS) and not (protect & PAGE_GUARD)
            if readable and size > 0:
                to_read = min(size, max_region_bytes)
                buf = ctypes.create_string_buffer(to_read)
                bytes_read = ctypes.c_size_t(0)
                ok = kernel32.ReadProcessMemory(
                    handle,
                    ctypes.c_void_p(base),
                    buf,
                    to_read,
                    ctypes.byref(bytes_read),
                )
                if ok and bytes_read.value:
                    data = buf.raw[: bytes_read.value]
                    scanned_regions += 1
                    scanned_bytes += len(data)
                    for hit in _scan_buffer(data, probes):
                        findings.append({
                            "region_start": hex(base),
                            "region_end": hex(base + size),
                            **hit,
                        })
            next_address = base + size
            if next_address <= address:
                break
            address = next_address
    finally:
        kernel32.CloseHandle(handle)
    return findings, scanned_regions, scanned_bytes


def _scan_once(pid: int, probes: list[EncodedProbe], max_region_bytes: int) -> tuple[list[dict[str, object]], int, int]:
    system = platform.system().lower()
    if system == "windows":
        return _scan_windows(pid, probes, max_region_bytes)
    if system == "linux":
        return _scan_linux(pid, probes, max_region_bytes)
    raise RuntimeError(f"Unsupported OS for direct memory scanning: {platform.system()}")


# ---------------------------------------------------------------------------
# v1.22c vector-search fragment probes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VectorSource:
    label: str
    vector: list[float]


def _coerce_float_vector(value: Any, *, label: str = "vector") -> list[float]:
    if isinstance(value, dict):
        for key in ("embedding", "vector", "query_vector", "values", "data"):
            if key in value:
                return _coerce_float_vector(value[key], label=f"{label}.{key}")
        raise ValueError(f"{label} JSON object has no embedding/vector field")
    if not isinstance(value, list | tuple):
        raise ValueError(f"{label} is not a list of floats")
    out = [float(x) for x in value]
    if not out:
        raise ValueError(f"{label} is empty")
    if not all(math.isfinite(x) for x in out):
        raise ValueError(f"{label} contains non-finite values")
    return out


def _load_vector_json(path: str | None, *, label: str) -> list[VectorSource]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("vectors"), list):
        return [
            VectorSource(f"{label}:{i}", _coerce_float_vector(item, label=f"{label}:{i}"))
            for i, item in enumerate(raw["vectors"])
        ]
    if isinstance(raw, list) and raw and isinstance(raw[0], (list, tuple, dict)):
        return [
            VectorSource(f"{label}:{i}", _coerce_float_vector(item, label=f"{label}:{i}"))
            for i, item in enumerate(raw)
        ]
    return [VectorSource(label, _coerce_float_vector(raw, label=label))]


def _load_f32_vector_file(path: str | None, *, label: str) -> list[VectorSource]:
    if not path:
        return []
    raw = Path(path).read_bytes()
    if len(raw) % 4:
        raise ValueError(f"{path} size is not a multiple of float32")
    values = list(struct.unpack("<" + "f" * (len(raw) // 4), raw))
    return [VectorSource(label, values)]


def _lmstudio_base_url(base_url: str) -> str:
    base = (base_url or "http://127.0.0.1:1234").rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _embed_query_with_lmstudio(*, base_url: str, model: str, text: str, timeout_seconds: float) -> VectorSource:
    body = json.dumps({"model": model, "input": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _lmstudio_base_url(base_url) + "/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError(f"LM Studio embeddings response missing data: {payload!r}")
    embedding = data[0].get("embedding") if isinstance(data[0], dict) else None
    vector = _coerce_float_vector(embedding, label="lmstudio.query.embedding")
    text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return VectorSource(f"query:lmstudio:{model}:{text_digest[:16]}", vector)


def _load_query_text_file(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


def _load_adapter_vault_vectors(
    *,
    vault: str | None,
    collection: str | None,
    ids: list[str] | None,
    max_vectors: int,
) -> list[VectorSource]:
    if not vault or not collection:
        return []
    root = Path(vault) / collection
    index_path = root / "index.jsonl"
    vectors_path = root / "vectors.f32"
    if not index_path.exists() or not vectors_path.exists():
        raise FileNotFoundError(f"Adapter vault collection not found: {root}")

    active: dict[str, dict[str, Any]] = {}
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        op = str(obj.get("op", "record"))
        record_id = str(obj.get("id", ""))
        if op in {"delete", "tombstone"}:
            active.pop(record_id, None)
            continue
        if record_id:
            active[record_id] = obj

    selected_ids = ids or sorted(active)[:max_vectors]
    out: list[VectorSource] = []
    with vectors_path.open("rb") as f:
        for record_id in selected_ids:
            if record_id not in active:
                continue
            row = active[record_id]
            offset = int(row["offset"])
            dim = int(row["dimension"])
            f.seek(offset)
            raw = f.read(dim * 4)
            if len(raw) != dim * 4:
                continue
            values = list(struct.unpack("<" + "f" * dim, raw))
            out.append(VectorSource(f"adapter-vault:{collection}:{record_id}", values))
            if len(out) >= max_vectors:
                break
    return out


def _float32_slab(vector: list[float]) -> bytes:
    return struct.pack("<" + "f" * len(vector), *[float(x) for x in vector])


def _float64_slab(vector: list[float]) -> bytes:
    return struct.pack("<" + "d" * len(vector), *[float(x) for x in vector])


def _shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts: dict[int, int] = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    n = float(len(data))
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _select_fragment_windows(raw: bytes, *, window_bytes: int, max_count: int, min_entropy: float) -> list[tuple[int, bytes, float]]:
    if window_bytes <= 0 or len(raw) < window_bytes or max_count <= 0:
        return []
    stride = max(1, window_bytes)
    candidates: list[tuple[float, int, bytes]] = []
    for offset in range(0, len(raw) - window_bytes + 1, stride):
        chunk = raw[offset : offset + window_bytes]
        # Avoid trivial all-zero or near-zero fragments that are not identifying.
        if len(set(chunk)) < 4:
            continue
        entropy = _shannon_entropy(chunk)
        if entropy >= min_entropy:
            candidates.append((entropy, offset, chunk))
    # Pick high-entropy windows across the slab, not just the first N.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    picked: list[tuple[int, bytes, float]] = []
    used_ranges: list[tuple[int, int]] = []
    for entropy, offset, chunk in candidates:
        end = offset + window_bytes
        if any(not (end <= a or offset >= b) for a, b in used_ranges):
            continue
        picked.append((offset, chunk, entropy))
        used_ranges.append((offset, end))
        if len(picked) >= max_count:
            break
    picked.sort(key=lambda item: item[0])
    return picked


def _vector_fragment_probes(
    sources: list[VectorSource],
    *,
    fragment_floats: int = 16,
    max_fragments_per_vector: int = 32,
    min_entropy: float = 3.0,
    include_f32: bool = True,
    include_f64: bool = True,
    include_b64: bool = True,
    include_ascii: bool = False,
) -> list[EncodedProbe]:
    out: list[EncodedProbe] = []
    for source in sources:
        vector = source.vector
        if not vector:
            continue
        vector_digest = hashlib.sha256(_float32_slab(vector)).hexdigest()
        vector_kind = "embedding_vector_fragment"
        if source.label.startswith("query"):
            vector_kind = "embedding_query_fragment"
        elif "vault" in source.label or "stored" in source.label:
            vector_kind = "embedding_stored_fragment"

        if include_f32:
            slab = _float32_slab(vector)
            for offset, chunk, entropy in _select_fragment_windows(
                slab,
                window_bytes=max(4, int(fragment_floats) * 4),
                max_count=max_fragments_per_vector,
                min_entropy=min_entropy,
            ):
                out.append(
                    EncodedProbe(
                        chunk,
                        vector_kind,
                        vector_digest,
                        "float32-le-fragment",
                        f"{source.label}:f32@{offset}:entropy={entropy:.2f}",
                    )
                )

        if include_f64:
            slab64 = _float64_slab(vector)
            for offset, chunk, entropy in _select_fragment_windows(
                slab64,
                window_bytes=max(8, int(fragment_floats) * 8),
                max_count=max(1, max_fragments_per_vector // 2),
                min_entropy=min_entropy,
            ):
                out.append(
                    EncodedProbe(
                        chunk,
                        vector_kind,
                        vector_digest,
                        "float64-le-fragment",
                        f"{source.label}:f64@{offset}:entropy={entropy:.2f}",
                    )
                )

        if include_b64:
            b64 = base64.b64encode(_float32_slab(vector))
            # base64 windows are longer to reduce accidental hits in JSON logs.
            b64_window = max(48, int(fragment_floats) * 6)
            for offset, chunk, entropy in _select_fragment_windows(
                b64,
                window_bytes=b64_window,
                max_count=max_fragments_per_vector,
                min_entropy=min_entropy,
            ):
                out.append(
                    EncodedProbe(
                        chunk,
                        vector_kind,
                        vector_digest,
                        "float32-base64-fragment",
                        f"{source.label}:b64@{offset}:entropy={entropy:.2f}",
                    )
                )

        if include_ascii:
            # Python/JSON decimal rendering varies; this catches long-lived debug
            # strings or non-base64 compatibility code, but is not treated as an
            # exhaustive representation.
            for i in range(0, max(0, len(vector) - fragment_floats + 1), max(1, fragment_floats)):
                chunk_values = vector[i : i + fragment_floats]
                ascii_window = ",".join(f"{x:.8g}" for x in chunk_values).encode("ascii", errors="ignore")
                if len(ascii_window) >= 32 and _shannon_entropy(ascii_window) >= min_entropy:
                    out.append(
                        EncodedProbe(
                            ascii_window,
                            vector_kind,
                            vector_digest,
                            "ascii-decimal-fragment",
                            f"{source.label}:ascii@float{i}",
                        )
                    )
                    if sum(1 for p in out if p.encoding == "ascii-decimal-fragment") >= max_fragments_per_vector:
                        break
    return _dedupe_probes(out)


def _run_during_command(command: str, cwd: str | None, timeout: float | None) -> dict[str, Any]:
    if not command:
        return {"started": False}
    started = time.time()
    try:
        # shell=True is intentional here: Windows/PowerShell forensic workflows
        # often require a single command line with backtick/quote semantics.  The
        # report stores only hashes of outputs to avoid sensitive leakage.
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd or None,
            text=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        stdout = proc.stdout or b""
        stderr = proc.stderr or b""
        return {
            "started": True,
            "returncode": int(proc.returncode),
            "duration_ms": round((time.time() - started) * 1000, 3),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        return {
            "started": True,
            "returncode": None,
            "timeout": True,
            "duration_ms": round((time.time() - started) * 1000, 3),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
        }
    except Exception as exc:
        return {
            "started": True,
            "returncode": None,
            "error": str(exc),
            "duration_ms": round((time.time() - started) * 1000, 3),
        }


def _scan_repeated(
    *,
    pid: int,
    probes: list[EncodedProbe],
    max_region_bytes: int,
    duration_seconds: float,
    interval_seconds: float,
    during_command: str = "",
    during_command_cwd: str = "",
    command_timeout_seconds: float | None = None,
) -> tuple[list[dict[str, object]], int, int, int, dict[str, Any]]:
    started = time.time()
    deadline = started + max(0.0, duration_seconds)
    findings: list[dict[str, object]] = []
    total_regions = 0
    total_bytes = 0
    passes = 0
    command_result: dict[str, Any] = {"started": False}

    # Do one pre-trigger scan so the report can distinguish "already resident"
    # from "appeared during query".
    while True:
        try:
            scan_findings, regions, scanned_bytes = _scan_once(pid, probes, max_region_bytes)
        except Exception:
            raise
        passes += 1
        total_regions += regions
        total_bytes += scanned_bytes
        phase = "during" if command_result.get("started") else "pre"
        for hit in scan_findings:
            h = dict(hit)
            h["scan_pass"] = passes
            h["scan_phase"] = phase
            findings.append(h)

        now = time.time()
        if during_command and not command_result.get("started"):
            command_result = _run_during_command(during_command, during_command_cwd or None, command_timeout_seconds)
            # Continue scanning for the remaining tail duration.
            now = time.time()

        if now >= deadline or not during_command and duration_seconds <= 0:
            break
        time.sleep(max(0.0, interval_seconds))

    # Deduplicate evidence while keeping the earliest phase/pass.
    deduped: list[dict[str, object]] = []
    seen: set[tuple[Any, ...]] = set()
    for hit in findings:
        key = (
            hit.get("region_start"),
            hit.get("probe_sha256"),
            hit.get("offset"),
            hit.get("encoding"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped, total_regions, total_bytes, passes, command_result


def _build_vector_sources_from_args(args: argparse.Namespace) -> list[VectorSource]:
    sources: list[VectorSource] = []
    query_text = str(args.query_text or "") or _load_query_text_file(args.query_text_file)
    if query_text:
        sources.append(
            _embed_query_with_lmstudio(
                base_url=str(args.lmstudio_url),
                model=str(args.embedding_model),
                text=query_text,
                timeout_seconds=float(args.lmstudio_timeout_seconds),
            )
        )
    for path in list(args.vector_json or []):
        sources.extend(_load_vector_json(path, label=f"vector-json:{Path(path).name}"))
    for path in list(args.vector_f32_file or []):
        sources.extend(_load_f32_vector_file(path, label=f"vector-f32:{Path(path).name}"))
    vault_ids = list(args.vector_id or [])
    sources.extend(
        _load_adapter_vault_vectors(
            vault=args.adapter_vault,
            collection=args.collection,
            ids=vault_ids or None,
            max_vectors=int(args.max_vault_vectors),
        )
    )
    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a running process for sensitive cleartext or embedding-vector probes.")
    parser.add_argument("--pid", type=int, required=True, help="Target mycelia_platform.py process id")
    parser.add_argument("--probe", action="append", default=[], help="Plaintext probe; may be repeated. Auto-classifies 64-hex values as public_identifier.")
    parser.add_argument("--probe-sensitive", action="append", default=[], help="Sensitive cleartext probe; strict hits block certification.")
    parser.add_argument("--probe-public", action="append", default=[], help="Public identifier probe; hits are reported but do not block strict certification.")
    parser.add_argument("--probe-audit", action="append", default=[], help="Audit artifact probe; hits are reported but do not block strict certification.")
    parser.add_argument("--probe-file", help="UTF-8 file with one sensitive plaintext probe per line")
    parser.add_argument("--operation", action="append", default=[], help="Operation observed during scan, e.g. login_attractor")
    parser.add_argument("--challenge-id", default="", help="Challenge id from residency_audit_manifest")
    parser.add_argument("--json-out", default="", help="Write report to this path")
    parser.add_argument("--max-region-bytes", type=int, default=64 * 1024 * 1024, help="Cap bytes per memory region")
    parser.add_argument("--canary-positive", action="append", default=[], help="Toxic canary expected to be found; proves scanner visibility / anti-evasion baseline.")
    parser.add_argument("--require-canary-positive", action="store_true", help="Fail the report unless all --canary-positive values are found at least once.")

    # v1.22c vector-search options.
    parser.add_argument("--during-smql-vector-search", action="store_true", help="Mark the report as a SMQL vector-search residency probe.")
    parser.add_argument("--during-command", default="", help="Command to execute while scanning, usually an adapter SMQL query. Output is hashed only.")
    parser.add_argument("--during-command-cwd", default="", help="Working directory for --during-command.")
    parser.add_argument("--during-command-timeout-seconds", type=float, default=60.0, help="Timeout for --during-command.")
    parser.add_argument("--scan-duration-seconds", type=float, default=0.0, help="Repeat scans for this duration; use 5-15s for live vector-search probes.")
    parser.add_argument("--scan-interval-ms", type=int, default=250, help="Delay between repeated scans.")
    parser.add_argument("--query-text", default="", help="Text query to embed through LM Studio and use as a query-vector probe. The text is not printed in the report.")
    parser.add_argument("--query-text-file", default="", help="UTF-8 file containing a text query to embed through LM Studio.")
    parser.add_argument("--lmstudio-url", default="http://127.0.0.1:1234", help="LM Studio base URL for --query-text probes.")
    parser.add_argument("--embedding-model", default="text-embedding-nomic-embed-text-v2-moe", help="LM Studio embedding model for --query-text probes.")
    parser.add_argument("--lmstudio-timeout-seconds", type=float, default=60.0, help="Timeout for LM Studio embedding probe generation.")
    parser.add_argument("--vector-json", action="append", default=[], help="JSON file containing one vector or a list/object of vectors.")
    parser.add_argument("--vector-f32-file", action="append", default=[], help="Raw little-endian float32 vector file.")
    parser.add_argument("--adapter-vault", default="", help="Path to SMQL adapter vault root, e.g. .smql_adapter.")
    parser.add_argument("--collection", default="", help="Collection inside --adapter-vault.")
    parser.add_argument("--vector-id", action="append", default=[], help="Active adapter-vault vector id to probe; repeatable. Defaults to first N active records.")
    parser.add_argument("--max-vault-vectors", type=int, default=8, help="Max active vault vectors to turn into probes.")
    parser.add_argument("--fragment-floats", type=int, default=16, help="Floats per binary fragment window.")
    parser.add_argument("--max-fragments-per-vector", type=int, default=32, help="Max high-entropy fragments per vector representation.")
    parser.add_argument("--min-fragment-entropy", type=float, default=3.0, help="Minimum Shannon entropy for vector fragment windows.")
    parser.add_argument("--no-f32-fragments", action="store_true", help="Disable raw float32 fragment probes.")
    parser.add_argument("--no-f64-fragments", action="store_true", help="Disable raw float64 fragment probes.")
    parser.add_argument("--no-b64-fragments", action="store_true", help="Disable base64 float32 fragment probes.")
    parser.add_argument("--ascii-decimal-fragments", action="store_true", help="Also scan approximate ASCII decimal windows.")
    args = parser.parse_args(argv)

    specs: list[ProbeSpec] = []
    specs.extend(ProbeSpec(value, _auto_probe_kind(value)) for value in list(args.probe))
    specs.extend(ProbeSpec(value, "sensitive_cleartext") for value in list(args.probe_sensitive))
    specs.extend(ProbeSpec(value, "public_identifier") for value in list(args.probe_public))
    specs.extend(ProbeSpec(value, "audit_artifact") for value in list(args.probe_audit))
    specs.extend(ProbeSpec(value, "probe_canary_positive") for value in list(args.canary_positive))
    specs.extend(ProbeSpec(value, "sensitive_cleartext") for value in _load_probe_file(args.probe_file))
    probes = _probe_bytes(specs)

    vector_sources: list[VectorSource] = []
    vector_probe_error = ""
    try:
        vector_sources = _build_vector_sources_from_args(args)
        probes.extend(
            _vector_fragment_probes(
                vector_sources,
                fragment_floats=int(args.fragment_floats),
                max_fragments_per_vector=int(args.max_fragments_per_vector),
                min_entropy=float(args.min_fragment_entropy),
                include_f32=not bool(args.no_f32_fragments),
                include_f64=not bool(args.no_f64_fragments),
                include_b64=not bool(args.no_b64_fragments),
                include_ascii=bool(args.ascii_decimal_fragments),
            )
        )
        probes = _dedupe_probes(probes)
    except Exception as exc:
        vector_probe_error = str(exc)

    if not probes and not vector_probe_error:
        parser.error(
            "At least one --probe, --probe-sensitive, --probe-public, --probe-audit, --probe-file, "
            "--vector-json, --vector-f32-file or --adapter-vault/--collection entry is required"
        )

    started = time.time()
    command_result: dict[str, Any] = {"started": False}
    scan_passes = 1
    try:
        duration = max(0.0, float(args.scan_duration_seconds))
        # A live command without explicit duration still needs a post-trigger pass.
        if args.during_command and duration <= 0:
            duration = 3.0
        if duration > 0 or args.during_command:
            findings, regions, scanned_bytes, scan_passes, command_result = _scan_repeated(
                pid=args.pid,
                probes=probes,
                max_region_bytes=args.max_region_bytes,
                duration_seconds=duration,
                interval_seconds=max(0.001, int(args.scan_interval_ms) / 1000.0),
                during_command=str(args.during_command or ""),
                during_command_cwd=str(args.during_command_cwd or ""),
                command_timeout_seconds=float(args.during_command_timeout_seconds),
            )
        else:
            findings, regions, scanned_bytes = _scan_once(args.pid, probes, args.max_region_bytes)
        status = "ok"
        error = ""
    except Exception as exc:
        findings, regions, scanned_bytes = [], 0, 0
        status = "error"
        error = str(exc)

    if vector_probe_error:
        status = "error"
        error = (error + "; " if error else "") + f"vector_probe_error: {vector_probe_error}"

    # Hash both UTF-8 and UTF-16LE forms, because both were scanned.
    probe_hashes = [p.hash for p in probes]
    probe_manifest = [
        {
            "probe_sha256": p.hash,
            "source_probe_sha256": p.source_hash,
            "probe_kind": p.kind,
            "strict_relevant": p.kind in SENSITIVE_KINDS,
            "encoding": p.encoding,
            "encoding_bytes": len(p.raw),
            "probe_label": p.label,
        }
        for p in probes
    ]
    hit_counts_by_kind: dict[str, int] = {}
    hit_counts_by_encoding: dict[str, int] = {}
    strict_hits = 0
    vector_hits = 0
    vector_strict_hits = 0
    for hit in findings:
        kind = str(hit.get("probe_kind", "sensitive_cleartext"))
        encoding = str(hit.get("encoding", "unknown"))
        hit_counts_by_kind[kind] = hit_counts_by_kind.get(kind, 0) + 1
        hit_counts_by_encoding[encoding] = hit_counts_by_encoding.get(encoding, 0) + 1
        strict_relevant = bool(hit.get("strict_relevant", kind in SENSITIVE_KINDS))
        if strict_relevant:
            strict_hits += 1
        if kind.startswith("embedding_"):
            vector_hits += 1
            if strict_relevant:
                vector_strict_hits += 1

    strict_negative = status == "ok" and strict_hits == 0 and regions > 0 and scanned_bytes > 0
    vector_probe_count = sum(1 for p in probes if p.kind.startswith("embedding_"))
    vector_negative = (
        status == "ok"
        and vector_probe_count > 0
        and vector_strict_hits == 0
        and regions > 0
        and scanned_bytes > 0
    )
    canary_hashes = {hashlib.sha256(v.encode("utf-8")).hexdigest() for v in list(args.canary_positive)}
    canary_hits = {str(hit.get("source_probe_sha256", "")) for hit in findings if str(hit.get("probe_kind", "")) == "probe_canary_positive"}
    canary_positive_ok = (not canary_hashes) or canary_hashes.issubset(canary_hits)
    if args.require_canary_positive and not canary_positive_ok and status == "ok":
        status = "error"
        error = (error + "; " if error else "") + "Required positive scanner canary was not observed."
        strict_negative = False
        vector_negative = False

    command_ok = not command_result.get("started") or command_result.get("returncode") == 0
    if args.during_command and not command_ok and status == "ok":
        status = "error"
        error = (error + "; " if error else "") + "Observed command failed or timed out."
        strict_negative = False
        vector_negative = False

    if args.during_smql_vector_search:
        if status != "ok":
            vector_verdict = "error"
        elif vector_probe_count <= 0:
            vector_verdict = "inconclusive:no-vector-probes"
        elif not canary_positive_ok:
            vector_verdict = "inconclusive:canary-missing"
        elif vector_strict_hits > 0:
            vector_verdict = "fail:vector-fragments-observed"
        elif not command_ok:
            vector_verdict = "inconclusive:query-command-failed"
        else:
            vector_verdict = "pass:external-probe-negative"
    else:
        vector_verdict = "not-run"

    report = {
        "status": status,
        "scanner_version": SCANNER_VERSION,
        "pid": args.pid,
        "challenge_id": args.challenge_id,
        "started_at": started,
        "finished_at": time.time(),
        "duration_ms": round((time.time() - started) * 1000, 3),
        "platform": platform.platform(),
        "operations": args.operation + (["smql_vector_search"] if args.during_smql_vector_search else []),
        "scan_passes": scan_passes,
        "probe_sha256": probe_hashes,
        "probe_manifest": probe_manifest,
        "hit_counts_by_kind": hit_counts_by_kind,
        "hit_counts_by_encoding": hit_counts_by_encoding,
        "hits": len(findings),
        "strict_hits": strict_hits,
        "non_strict_hits": max(0, len(findings) - strict_hits),
        "negative": strict_negative,
        "strict_negative": strict_negative,
        "raw_negative": status == "ok" and len(findings) == 0 and regions > 0 and scanned_bytes > 0,
        "scanned_regions": regions,
        "scanned_bytes": scanned_bytes,
        "canary_positive_required": bool(args.require_canary_positive),
        "canary_positive_ok": canary_positive_ok,
        "canary_expected_count": len(canary_hashes),
        "canary_hit_count": len(canary_hits),
        "vector_search_probe": {
            "enabled": bool(args.during_smql_vector_search),
            "verdict": vector_verdict,
            "vector_sources": [
                {
                    "label_sha256": hashlib.sha256(src.label.encode("utf-8")).hexdigest(),
                    "label_hint": src.label.split(":")[0],
                    "dimension": len(src.vector),
                    "vector_f32_sha256": hashlib.sha256(_float32_slab(src.vector)).hexdigest(),
                }
                for src in vector_sources
            ],
            "vector_source_count": len(vector_sources),
            "vector_fragment_probe_count": vector_probe_count,
            "vector_fragment_hits": vector_hits,
            "vector_strict_hits": vector_strict_hits,
            "vector_negative": vector_negative,
            "fragment_floats": int(args.fragment_floats),
            "max_fragments_per_vector": int(args.max_fragments_per_vector),
            "representations": {
                "float32_le": not bool(args.no_f32_fragments),
                "float64_le": not bool(args.no_f64_fragments),
                "float32_base64": not bool(args.no_b64_fragments),
                "ascii_decimal": bool(args.ascii_decimal_fragments),
            },
            "strict_no_cpu_ram_external_probe_passed": bool(
                args.during_smql_vector_search and vector_negative and canary_positive_ok and command_ok
            ),
            "proof_boundary": (
                "external-negative-fragment-scan-only; combine with sealed native ABI attestation "
                "before setting strict_vram_residency_proven=true"
            ),
        },
        "during_command": command_result,
        "findings": findings[:100],
        "truncated_findings": max(0, len(findings) - 100),
        "error": error,
    }
    report["evidence_digest"] = hashlib.sha256(
        json.dumps({k: v for k, v in report.items() if k != "evidence_digest"}, sort_keys=True).encode("utf-8")
    ).hexdigest()

    raw = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(raw, encoding="utf-8")
    print(raw)
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())

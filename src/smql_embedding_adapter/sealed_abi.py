"""Sealed native ABI contracts for SMQL/MyceliaDB v1.22c.

This module intentionally separates *native vector search* from *forensic
zero-host-copy proof*.

v1.22b already executes full-dimensional ranking inside MyceliaDB and can use an
OpenCL device buffer.  v1.22c adds a sealed ABI contract: the Python layer may
request sealed operation, but it must not claim strict no-CPU-RAM residency
unless the native library returns an attestation with all required proof flags.

The adapter therefore has three grades:

- ``opencl-vram``: native vector search, but vectors may have crossed Python/HTTP.
- ``sealed-abi-active``: a native ABI answered the request.
- ``strict-vram-residency-proven``: the native ABI attested no host vector copy,
  zeroized staging, VRAM residency, and kernel identity.

The last grade is deliberately fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class SealedMode(StrEnum):
    OFF = "off"
    AUTO = "auto"
    REQUIRED = "required"


REQUIRED_PROOF_FLAGS = frozenset(
    {
        "vram_resident",
        "no_host_vector_copy",
        "host_staging_zeroized",
        "kernel_identity_attested",
        "driver_device_bound",
    }
)


@dataclass(slots=True, frozen=True)
class SealedAbiAttestation:
    """Normalized v1.22c attestation payload returned by MyceliaDB."""

    status: str = "unknown"
    abi_version: str = ""
    sealed_abi_active: bool = False
    strict_vram_residency_proven: bool = False
    proof_flags: frozenset[str] = field(default_factory=frozenset)
    proof_id: str = ""
    proof_mac: str = ""
    transport_grade: str = "unknown"
    reason: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SealedAbiAttestation":
        raw = dict(data or {})
        flags_raw = raw.get("proof_flags", [])
        flags: set[str] = set()
        if isinstance(flags_raw, list | tuple | set):
            flags = {str(x) for x in flags_raw}
        elif isinstance(flags_raw, str):
            flags = {x.strip() for x in flags_raw.split(",") if x.strip()}

        strict = bool(raw.get("strict_vram_residency_proven", False))
        if not strict:
            strict = REQUIRED_PROOF_FLAGS.issubset(flags) and bool(raw.get("sealed_abi_active"))

        return cls(
            status=str(raw.get("status", "unknown")),
            abi_version=str(raw.get("abi_version", raw.get("version", ""))),
            sealed_abi_active=bool(raw.get("sealed_abi_active", False)),
            strict_vram_residency_proven=strict,
            proof_flags=frozenset(flags),
            proof_id=str(raw.get("proof_id", "")),
            proof_mac=str(raw.get("proof_mac", "")),
            transport_grade=str(raw.get("transport_grade", "unknown")),
            reason=str(raw.get("reason", raw.get("message", ""))),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "abi_version": self.abi_version,
            "sealed_abi_active": self.sealed_abi_active,
            "strict_vram_residency_proven": self.strict_vram_residency_proven,
            "proof_flags": sorted(self.proof_flags),
            "proof_id": self.proof_id,
            "proof_mac": self.proof_mac,
            "transport_grade": self.transport_grade,
            "reason": self.reason,
        }

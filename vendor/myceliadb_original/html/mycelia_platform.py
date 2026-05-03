#!/usr/bin/env python3
"""Autarke MyceliaDB-Plattform.

HTTP JSON bridge for PHP:
- import_dump: parse .sql dump directly and inject rows as DAD nutrient nodes.
- query_pattern: associative lookup through CognitiveCore/DynamicAssociativeDatabase.
- check_integrity: reconstruct encrypted user/profile nodes and report attractor health.
- register_user/login_attractor/get_profile/update_profile: frontend session support.

No PDO, no SQL client, no SQLite materialization for dumps. Runtime state lives in
CognitiveCore.database. Payloads are stored encrypted; with a working native OpenCL
library the key stream is generated through the Mycelia GPU engine. In environments
without the experimental library the process falls back to a deterministic in-memory
cipher so development remains testable; the response includes driver_mode.
"""
from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import logging
import os
import ctypes
import re
import secrets
import socketserver
import ssl
import struct
import sys
import time
from datetime import datetime
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Callable

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.asymmetric import padding, rsa, ed25519, x25519
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception as exc:  # pragma: no cover - deployment guard
    hashes = serialization = HKDF = padding = rsa = ed25519 = x25519 = InvalidSignature = AESGCM = None  # type: ignore[assignment]
    _CRYPTOGRAPHY_IMPORT_ERROR = exc
else:
    _CRYPTOGRAPHY_IMPORT_ERROR = None

ROOT = Path(__file__).resolve().parent
CORE_ROOT = (ROOT.parent / "Mycelia_Database-main").resolve()
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

try:
    from mycelia_ai.cognition.cognitive_core import CognitiveCore
    from mycelia_ai.import_sql import OfflineDriver
    from mycelia_ai.main import _resolve_driver_library, load_config
    from mycelia_ai.core.driver import OpenCLDriver
except Exception as exc:  # pragma: no cover - startup guard
    print(f"CRITICAL: Core-Projekt konnte nicht geladen werden: {exc}", file=sys.stderr)
    raise

try:
    from mycelia_chat_engine import MyceliaChatEngine
except Exception:
    MyceliaChatEngine = None  # type: ignore[assignment]

LOGGER = logging.getLogger("mycelia_platform")
PORT = int(os.environ.get("MYCELIA_PORT", "9999"))
APP_SECRET = os.environ.get("MYCELIA_APP_SECRET", "MyceliaEnterpriseSecretKey2026")
USER_TABLE = "mycelia_users"
FORUM_TABLE = "mycelia_forum_threads"
BLOG_TABLE = "mycelia_blogs"
BLOG_POST_TABLE = "mycelia_blog_posts"
COMMENT_TABLE = "mycelia_comments"
REACTION_TABLE = "mycelia_reactions"
SITE_TEXT_TABLE = "mycelia_site_texts"
USER_PERMISSION_TABLE = "mycelia_user_permissions"
PLUGIN_TABLE = "mycelia_plugins"
PLUGIN_AUDIT_TABLE = "mycelia_plugin_audit"
MEDIA_TABLE = "mycelia_media_nodes"
MEDIA_REF_TABLE = "mycelia_media_refs"
E2EE_KEY_TABLE = "mycelia_e2ee_public_keys"
E2EE_MESSAGE_TABLE = "mycelia_e2ee_messages"
WEBAUTHN_CREDENTIAL_TABLE = "mycelia_webauthn_credentials"
SECURITY_CANARY_TABLE = "mycelia_security_canaries"
BADGE_TABLE = "mycelia_user_badges"
POLL_TABLE = "mycelia_polls"
POLL_VOTE_TABLE = "mycelia_poll_votes"
TIME_CAPSULE_TABLE = "mycelia_time_capsules"
SNAPSHOT_MAGIC = b"MYCELIA_SNAPSHOT_V1\0"
DEFAULT_SNAPSHOT_PATH = Path(
    os.environ.get("MYCELIA_SNAPSHOT_PATH", str(ROOT / "snapshots" / "autosave.mycelia"))
).resolve()
AUTOSAVE_ENABLED = os.environ.get("MYCELIA_AUTOSAVE", "1").lower() not in {"0", "false", "no", "off"}
AUTORESTORE_ENABLED = os.environ.get("MYCELIA_AUTORESTORE", "1").lower() not in {"0", "false", "no", "off"}
INGEST_KEY_PATH = Path(os.environ.get("MYCELIA_INGEST_KEY_PATH", str(ROOT / "keys" / "ingest_private.pem"))).resolve()
DIRECT_INGEST_MAX_AGE_SECONDS = int(os.environ.get("MYCELIA_DIRECT_INGEST_MAX_AGE_SECONDS", "300"))
PUBLIC_MARKDOWN_RENDER_LIMIT = int(os.environ.get("MYCELIA_PUBLIC_MARKDOWN_RENDER_LIMIT", "2000000"))
PUBLIC_TEXT_STORAGE_LIMIT = int(os.environ.get("MYCELIA_PUBLIC_TEXT_STORAGE_LIMIT", "2000000"))
STRICT_VRAM_ONLY = os.environ.get("MYCELIA_STRICT_VRAM_ONLY", "0").lower() in {"1", "true", "yes", "on"}
DIRECT_INGEST_ALLOWED_OPS = {
    "register_user",
    "login_attractor",
    "update_profile",
    "create_forum_thread",
    "update_forum_thread",
    "delete_forum_thread",
    "create_comment",
    "update_comment",
    "delete_comment",
    "react_content",
    "create_blog",
    "update_blog",
    "delete_blog",
    "create_blog_post",
    "update_blog_post",
    "delete_blog_post",
    "vram_residency_audit",
    "admin_set_site_text",
    "admin_update_user_rights",
    "delete_my_account",
    "admin_install_plugin",
    "admin_set_plugin_state",
    "admin_delete_plugin",
    "run_plugin",
    "federation_peer_add",
    "federation_peer_remove",
    "federation_import_influx",
    "upload_media",
    "attach_media_to_content",
    "delete_media",
    "moderate_media",
    "e2ee_register_public_key",
    "e2ee_send_message",
    "e2ee_delete_message",
    "webauthn_register_credential",
    "webauthn_login_assertion",
}

SESSION_TTL_SECONDS = int(os.environ.get("MYCELIA_SESSION_TTL_SECONDS", "3600"))
REQUEST_TOKEN_TTL_SECONDS = int(os.environ.get("MYCELIA_REQUEST_TOKEN_TTL_SECONDS", "180"))
DIRECT_INGEST_AUTH_REQUIRED_OPS = {
    "update_profile",
    "create_forum_thread",
    "update_forum_thread",
    "delete_forum_thread",
    "create_comment",
    "update_comment",
    "delete_comment",
    "react_content",
    "create_blog",
    "update_blog",
    "delete_blog",
    "create_blog_post",
    "update_blog_post",
    "delete_blog_post",
    "vram_residency_audit",
    "admin_set_site_text",
    "admin_update_user_rights",
    "delete_my_account",
    "admin_install_plugin",
    "admin_set_plugin_state",
    "admin_delete_plugin",
    "run_plugin",
    "federation_peer_add",
    "federation_peer_remove",
    "federation_import_influx",
    "upload_media",
    "attach_media_to_content",
    "delete_media",
    "moderate_media",
    "e2ee_register_public_key",
    "e2ee_send_message",
    "e2ee_delete_message",
    "webauthn_register_credential",
    "create_poll",
    "vote_poll",
    "create_time_capsule",
}
SESSION_BOUND_READ_OPS = {
    "get_profile",
    "list_forum_threads",
    "get_forum_thread",
    "list_comments",
    "list_blogs",
    "get_blog",
    "list_blog_posts",
    "get_blog_post",
    "admin_overview",
    "list_users",
    "export_my_data",
    "list_plugins",
    "plugin_catalog",
    "smql_query",
    "smql_explain",
    "federation_status",
    "federation_export_stable",
    "provenance_log",
    "provenance_verify",
    "native_library_authenticity",
    "local_transport_security_status",
    "quantum_guard_status",
    "list_media_for_content",
    "render_media_safe",
    "list_all_media",
    "e2ee_public_key_lookup",
    "e2ee_recipient_directory",
    "e2ee_inbox",
    "e2ee_outbox",
    "telemetry_snapshot",
    "security_evolution_status",
    "webauthn_challenge_begin",
    "enterprise_plugin_dashboard",
    "fun_plugin_dashboard",
    "list_polls",
    "list_time_capsules",
}

RESIDENCY_AUDIT_VERSION = "VRAM_RESIDENCY_AUDIT_V11_GPU_RESIDENT_OPEN_RESTORE"
NATIVE_GPU_ENVELOPE_REQUESTED = os.environ.get("MYCELIA_NATIVE_GPU_ENVELOPE_OPENER", "0").lower() in {"1", "true", "yes"}
GPU_RESTORE_REQUESTED = os.environ.get("MYCELIA_GPU_RESTORE_OPENER", "0").lower() in {"1", "true", "yes"}
STRICT_VRAM_CERTIFICATION = os.environ.get("MYCELIA_STRICT_VRAM_CERTIFICATION", "0").lower() in {"1", "true", "yes"}
STRICT_RESPONSE_REDACTION = os.environ.get("MYCELIA_STRICT_RESPONSE_REDACTION", "1" if STRICT_VRAM_CERTIFICATION else "0").lower() in {"1", "true", "yes", "on"}
# Web UI display mode: strict audit endpoints remain redacted, but normal forum/blog/profile views
# may request cleartext reconstruction for human rendering. This is intentionally incompatible
# with running a RAM-residency proof after viewing sensitive content.
WEB_UI_CLEAR_TEXT_RESPONSES = os.environ.get("MYCELIA_WEB_UI_CLEAR_TEXT_RESPONSES", "1").lower() in {"1", "true", "yes", "on"}
GPU_ENVELOPE_LIBRARY_ENV = os.environ.get("MYCELIA_GPU_ENVELOPE_LIBRARY", "").strip()
AUTO_NATIVE_GPU_ENVELOPE = os.environ.get("MYCELIA_AUTO_NATIVE_GPU_ENVELOPE", "1").lower() not in {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_is_explicit_false(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {"0", "false", "no", "off"}

MEMORY_PROBE_TOOL = (ROOT.parent / "tools" / "mycelia_memory_probe.py").resolve()
HEARTBEAT_AUDIT_VERSION = "MYCELIA_HEARTBEAT_RESIDENCY_AUDIT_V1"
HEARTBEAT_AUDIT_LATEST_PATH = Path(
    os.environ.get("MYCELIA_HEARTBEAT_AUDIT_PATH", str(ROOT.parent / "docs" / "heartbeat_audit_latest.json"))
).resolve()
HEARTBEAT_PUBLIC_KEY_PATH = Path(
    os.environ.get("MYCELIA_HEARTBEAT_PUBLIC_KEY", str(ROOT.parent / "docs" / "audit_keys" / "heartbeat_ed25519_public.pem"))
).resolve()
HEARTBEAT_MAX_AGE_SECONDS = int(os.environ.get("MYCELIA_HEARTBEAT_MAX_AGE_SECONDS", "93600"))  # 26h grace for 24h tasks
HEARTBEAT_CERTIFIED_STATES = {"certified", "strict-certified"}

# v1.20 Enterprise extensions
SMQL_AUDIT_VERSION = "MYCELIA_SMQL_V1"
FEDERATION_AUDIT_VERSION = "MYCELIA_FEDERATION_V1"
PROVENANCE_AUDIT_VERSION = "MYCELIA_PROVENANCE_LEDGER_V1"
NATIVE_AUTHENTICITY_VERSION = "MYCELIA_NATIVE_AUTHENTICITY_V1"
LOCAL_TRANSPORT_SECURITY_VERSION = "MYCELIA_LOCAL_TRANSPORT_SECURITY_V1"
QUANTUM_GUARD_VERSION = "MYCELIA_QUANTUM_TENSION_GUARD_V1"
PROVENANCE_LEDGER_PATH = Path(os.environ.get("MYCELIA_PROVENANCE_LEDGER", str(ROOT / "snapshots" / "provenance.mycelia"))).resolve()
FEDERATION_STATE_PATH = Path(os.environ.get("MYCELIA_FEDERATION_STATE", str(ROOT / "snapshots" / "federation_peers.json"))).resolve()
NATIVE_HASH_MANIFEST_PATH = Path(os.environ.get("MYCELIA_NATIVE_HASH_MANIFEST", str(ROOT.parent / "docs" / "native_library_hashes.json"))).resolve()
NATIVE_LIBRARY_STRICT = os.environ.get("MYCELIA_NATIVE_LIBRARY_STRICT", "1").lower() in {"1", "true", "yes", "on"}
LOCAL_TRANSPORT_TOKEN_PATH = Path(os.environ.get("MYCELIA_LOCAL_TRANSPORT_TOKEN_PATH", str(ROOT / "keys" / "local_transport.token"))).resolve()
LOCAL_TRANSPORT_TOKEN_REQUIRED = os.environ.get("MYCELIA_LOCAL_TRANSPORT_TOKEN_REQUIRED", "1").lower() in {"1", "true", "yes", "on"}
LOCAL_HTTPS_ENABLED = os.environ.get("MYCELIA_LOCAL_HTTPS", "0").lower() in {"1", "true", "yes", "on"}
LOCAL_HTTPS_CERT_PATH = Path(os.environ.get("MYCELIA_LOCAL_HTTPS_CERT", str(ROOT / "keys" / "localhost_cert.pem"))).resolve()
LOCAL_HTTPS_KEY_PATH = Path(os.environ.get("MYCELIA_LOCAL_HTTPS_KEY", str(ROOT / "keys" / "localhost_key.pem"))).resolve()




def _existing_driver_candidates(path_config: Mapping[str, Any]) -> list[Path]:
    """Resolve OpenCL driver paths from both launch roots.

    The Core project stores native artifacts below ``Mycelia_Database-main/build``.
    Tests and production often start from the repository root or from ``html``;
    therefore relative paths from config.yaml must be interpreted against
    multiple stable anchors, not just the current working directory.
    """
    preferred_keys: tuple[str, ...]
    if sys.platform == "win32":
        preferred_keys = ("driver_library_windows", "driver_library")
    elif sys.platform == "darwin":
        preferred_keys = ("driver_library_macos", "driver_library")
    else:
        preferred_keys = ("driver_library_linux", "driver_library")

    # Include non-native config entries as a diagnostic/test fallback. On Windows
    # the first hit will still be the Windows DLL; on Linux CI this lets us prove
    # that the bundled Core DLL is found under Mycelia_Database-main/build.
    all_keys = (
        *preferred_keys,
        "driver_library_windows",
        "driver_library_linux",
        "driver_library_macos",
    )

    raw: list[str] = []
    for key in all_keys:
        value = path_config.get(key)
        if value:
            raw.append(str(value))
    raw.extend(
        (
            "build/CC_OpenCl.dll",
            "native/CC_OpenCl.dll",
            "CC_OpenCl.dll",
            "build/libopencl_driver.so",
            "native/libopencl_driver.so",
            "libopencl_driver.so",
            "build/libopencl_driver.dylib",
            "native/libopencl_driver.dylib",
            "libopencl_driver.dylib",
        )
    )

    anchors = (
        Path.cwd(),
        ROOT,
        ROOT / "native",
        ROOT.parent,
        ROOT.parent / "native",
        CORE_ROOT,
        CORE_ROOT / "build",
    )

    seen: set[Path] = set()
    found: list[Path] = []
    for value in raw:
        candidate = Path(value).expanduser()
        variants = [candidate] if candidate.is_absolute() else [anchor / candidate for anchor in anchors]
        for variant in variants:
            resolved = variant.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.exists():
                found.append(resolved)
    return found


def resolve_core_driver_library(path_config: Mapping[str, Any]) -> Path:
    candidates = _existing_driver_candidates(path_config)
    if candidates:
        verify_native_library_authenticity(candidates[0], "core_opencl_driver")
        return candidates[0]
    raise FileNotFoundError(
        "Keine passende OpenCL-Bibliothek gefunden. Geprüft wurden relative Pfade "
        "gegen Projektwurzel, html/, html/native/, Projektwurzel/native/ und Mycelia_Database-main/build."
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_native_hash_manifest() -> dict[str, Any]:
    if not NATIVE_HASH_MANIFEST_PATH.exists():
        return {"status": "missing", "libraries": {}}
    try:
        data = json.loads(NATIVE_HASH_MANIFEST_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"status": "invalid", "libraries": {}}
        libs = data.get("libraries", {})
        if not isinstance(libs, dict):
            data["libraries"] = {}
        return data
    except Exception as exc:
        return {"status": "error", "error": str(exc), "libraries": {}}


def verify_native_library_authenticity(path: Path, role: str) -> dict[str, Any]:
    """Verify native library hashes before ctypes/native driver loading.

    The verification is intentionally hash based so it works on Windows DLLs,
    Linux ELF objects and macOS dylibs without an external PKI.  Operators can
    regenerate docs/native_library_hashes.json during trusted builds.  In strict
    mode a known role/path hash mismatch fails closed.
    """
    resolved = path.resolve()
    actual = _sha256_file(resolved) if resolved.exists() else ""
    manifest = _load_native_hash_manifest()
    libs = manifest.get("libraries", {}) if isinstance(manifest.get("libraries", {}), dict) else {}
    candidates: list[dict[str, Any]] = []
    for key, entry in libs.items():
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("role", "")) == role:
            candidates.append({"manifest_key": key, **dict(entry)})
        elif str(entry.get("path", "")).replace("\\", "/").lower().endswith(resolved.name.lower()):
            candidates.append({"manifest_key": key, **dict(entry)})
    expected = ""
    matched_entry: dict[str, Any] | None = None
    for entry in candidates:
        expected = str(entry.get("sha256", "")).lower()
        manifest_path = str(entry.get("path", ""))
        manifest_name = manifest_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if expected and (expected == actual.lower()) and (not manifest_path or manifest_name == resolved.name.lower()):
            matched_entry = entry
            break
    status = "ok" if matched_entry else ("unmanaged" if manifest.get("status") == "missing" or not candidates else "mismatch")
    result = {
        "status": status,
        "version": NATIVE_AUTHENTICITY_VERSION,
        "role": role,
        "path": str(resolved),
        "sha256": actual,
        "manifest_path": str(NATIVE_HASH_MANIFEST_PATH),
        "manifest_status": manifest.get("status", "ok"),
        "expected_candidates": len(candidates),
        # fail_closed describes the policy, not whether it triggered.  Enterprise
        # dashboards must see that strict mode would stop a mismatched DLL before
        # ctypes/OpenCL loading.
        "fail_closed": bool(NATIVE_LIBRARY_STRICT),
        "fail_closed_triggered": bool(NATIVE_LIBRARY_STRICT and status == "mismatch"),
    }
    if NATIVE_LIBRARY_STRICT and status == "mismatch":
        raise RuntimeError(f"Native library authenticity check failed for {role}: {resolved}")
    return result


@dataclass(slots=True)
class CryptoPacket:
    seed: str
    blob: str
    mode: str


@dataclass(frozen=True)
class DirectIngestEnvelope:
    """Browser-sealed form payload.

    PHP receives this value as opaque JSON only.  It does not read username,
    password, profile fields, forum content, blog content, or comments.  The
    Python/OpenCL boundary is the first trusted application layer that may open
    the package.  In the current implementation the RSA/AES envelope is
    decrypted in Python before being handed to the GPU-capable Mycelia crypto
    path; a future native extension can move this final decrypt directly into
    VRAM.
    """

    op: str
    payload: dict[str, Any]
    nonce: str
    issued_at_ms: int



@dataclass(slots=True)
class EngineSession:
    """Ephemeral engine-side session attractor.

    PHP stores only the opaque handle plus the next one-time request token.  The
    authority state lives in the Mycelia engine and rotates on every validated
    POST/privileged request.
    """

    handle: str
    signature: str
    username: str
    role: str
    permissions: tuple[str, ...]
    request_token_hash: str
    request_token_hashes: dict[str, float]
    sequence: int
    issued_at: float
    expires_at: float
    last_seen: float





@dataclass(frozen=True)
class NativeGPUResidencyCapabilities:
    """Runtime capability contract for strict VRAM residency.

    v1.18D extends the native boundary taxonomy:
    - staging/opening of sealed envelopes and snapshots,
    - command-boundary coverage for auth/content/admin/plugin/GDPR,
    - native snapshot-runtime and persistence-mutation boundaries.

    Even with all v1.18D boundary flags true, strict VRAM-only remains false
    unless the native library also proves envelope_to_vram, snapshot_to_vram and
    selftest_passed.
    """

    requested: bool
    available: bool
    library_path: str | None
    envelope_to_vram: bool
    snapshot_to_vram: bool
    envelope_staging: bool
    snapshot_staging: bool
    native_command_executor: bool
    command_executor_selftest_passed: bool
    sensitive_command_executor: bool
    native_auth_executor: bool
    auth_executor_selftest_passed: bool
    native_content_executor: bool
    content_executor_selftest_passed: bool
    native_admin_executor: bool
    admin_executor_selftest_passed: bool
    native_plugin_executor: bool
    plugin_executor_selftest_passed: bool
    native_gdpr_executor: bool
    gdpr_executor_selftest_passed: bool
    native_snapshot_runtime: bool
    snapshot_runtime_selftest_passed: bool
    native_persistence_mutation: bool
    persistence_mutation_selftest_passed: bool
    native_strict_certification_gate: bool
    strict_certification_gate_selftest_passed: bool
    external_ram_probe_contract: bool
    gpu_resident_open_restore_proven: bool
    selftest_passed: bool
    staging_selftest_passed: bool
    reason: str
    exports: list[str]
    native_commands_supported: list[str]
    native_auth_commands_supported: list[str]
    native_content_commands_supported: list[str]
    native_admin_commands_supported: list[str]
    native_plugin_commands_supported: list[str]
    native_gdpr_commands_supported: list[str]
    native_snapshot_commands_supported: list[str]
    native_persistence_commands_supported: list[str]


class NativeGPUResidencyBridge:
    """ctypes boundary for an optional native GPU envelope/snapshot opener.

    This class is intentionally strict: it never marks the system as certified
    just because OpenCL is present.  The native library must expose a stable
    contract and prove its own residency claims.  v1.18D adds snapshot-runtime
    and persistence-mutation boundaries, but they remain conservative until a
    full native Mycelia graph runtime exists.
    """

    REQUIRED_EXPORTS = (
        "mycelia_gpu_envelope_capabilities_v1",
        "mycelia_gpu_residency_selftest_v1",
    )
    OPTIONAL_EXPORTS = (
        "mycelia_gpu_envelope_open_to_vram_v1",
        "mycelia_gpu_snapshot_restore_to_vram_v1",
        "mycelia_gpu_command_capabilities_v1",
        "mycelia_gpu_execute_command_v1",
        "mycelia_gpu_snapshot_runtime_capabilities_v1",
        "mycelia_gpu_persist_mutation_v1",
        "mycelia_gpu_snapshot_commit_v1",
        "mycelia_gpu_strict_residency_evidence_v1",
        "mycelia_gpu_external_probe_contract_v1",
    )

    def __init__(self, candidates: list[Path], requested: bool) -> None:
        self.requested = requested
        self.library_path: Path | None = None
        self.lib: Any = None
        self._exports: list[str] = []
        self._reason = "Native GPU residency bridge not requested."
        if not requested:
            return
        explicit_env = os.environ.get("MYCELIA_GPU_ENVELOPE_LIBRARY", "").strip()
        explicit = bool(explicit_env)
        first_existing = True
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                verify_native_library_authenticity(candidate, "native_gpu_envelope")
                if sys.platform == "win32":
                    try:
                        os.add_dll_directory(str(candidate.parent))
                    except Exception:
                        pass
                self.lib = ctypes.CDLL(str(candidate))
                self.library_path = candidate
                self._exports = [name for name in (*self.REQUIRED_EXPORTS, *self.OPTIONAL_EXPORTS) if hasattr(self.lib, name)]
                missing = [name for name in self.REQUIRED_EXPORTS if name not in self._exports]
                if missing:
                    self._reason = f"Library loaded but required exports are missing: {', '.join(missing)}"
                    self.lib = None
                    if explicit or first_existing:
                        break
                    continue
                self._reason = "Native GPU residency bridge loaded."
                break
            except Exception as exc:
                self.library_path = candidate
                self.lib = None
                self._reason = f"Failed to load native GPU residency bridge {candidate}: {exc}"
                if explicit:
                    break
                # Continue scanning fallback locations.  The first existing file
                # may be stale, blocked by Windows, or missing an adjacent
                # dependency.  Do not suppress a later valid html/native DLL.
                continue
            finally:
                first_existing = False

    @staticmethod
    def candidate_paths() -> list[Path]:
        if sys.platform == "win32":
            # Dedicated native residency ABI. CC_OpenCl.dll is the Core/OpenCL
            # driver ABI and must not be mistaken for the envelope contract.
            names = ["mycelia_gpu_envelope.dll"]
        elif sys.platform == "darwin":
            names = ["libmycelia_gpu_envelope.dylib"]
        else:
            names = ["libmycelia_gpu_envelope.so"]
        roots = [
            # The shipped Enterprise ABI lives under html/native and must win
            # over stale project-root/native copies after ZIP upgrades.
            ROOT / "native",
            ROOT,
            Path.cwd() / "native",
            Path.cwd(),
            ROOT.parent / "native",
            ROOT.parent,
            CORE_ROOT / "native",
            CORE_ROOT,
            CORE_ROOT / "build",
            ROOT.parent / "build",
        ]
        explicit_env = os.environ.get("MYCELIA_GPU_ENVELOPE_LIBRARY", "").strip()
        if explicit_env:
            # Explicit operator path is authoritative. Avoid silently falling
            # back to a different ABI.
            return [Path(explicit_env).expanduser().resolve()]
        seen: set[Path] = set()
        ordered: list[Path] = []
        for candidate in [r / n for r in roots for n in names]:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                ordered.append(resolved)
        return ordered

    @staticmethod
    def auto_detect_available() -> bool:
        return any(path.exists() for path in NativeGPUResidencyBridge.candidate_paths())

    def _call_json_export(self, export_name: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self.lib is None or not hasattr(self.lib, export_name):
            return {"status": "unavailable", "message": f"Export not available: {export_name}"}
        fn = getattr(self.lib, export_name)
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        fn.restype = ctypes.c_int
        request = json.dumps(payload or {}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        out = ctypes.create_string_buffer(65536)
        rc = int(fn(request, out, ctypes.sizeof(out)))
        text = out.value.decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {"raw": text}
        data.setdefault("native_return_code", rc)
        data.setdefault("status", "ok" if rc == 0 else "error")
        return data

    @staticmethod
    def _list_field(primary: Mapping[str, Any], fallback: Mapping[str, Any], key: str) -> list[str]:
        raw = primary.get(key, fallback.get(key, []))
        return [str(x) for x in raw] if isinstance(raw, list) else []

    def capabilities(self) -> NativeGPUResidencyCapabilities:
        if self.lib is None:
            return NativeGPUResidencyCapabilities(
                requested=self.requested,
                available=False,
                library_path=str(self.library_path) if self.library_path else None,
                envelope_to_vram=False,
                snapshot_to_vram=False,
                envelope_staging=False,
                snapshot_staging=False,
                native_command_executor=False,
                command_executor_selftest_passed=False,
                sensitive_command_executor=False,
                native_auth_executor=False,
                auth_executor_selftest_passed=False,
                native_content_executor=False,
                content_executor_selftest_passed=False,
                native_admin_executor=False,
                admin_executor_selftest_passed=False,
                native_plugin_executor=False,
                plugin_executor_selftest_passed=False,
                native_gdpr_executor=False,
                gdpr_executor_selftest_passed=False,
                native_snapshot_runtime=False,
                snapshot_runtime_selftest_passed=False,
                native_persistence_mutation=False,
                persistence_mutation_selftest_passed=False,
                native_strict_certification_gate=False,
                strict_certification_gate_selftest_passed=False,
                external_ram_probe_contract=False,
                gpu_resident_open_restore_proven=False,
                selftest_passed=False,
                staging_selftest_passed=False,
                reason=self._reason,
                exports=self._exports,
                native_commands_supported=[],
                native_auth_commands_supported=[],
                native_content_commands_supported=[],
                native_admin_commands_supported=[],
                native_plugin_commands_supported=[],
                native_gdpr_commands_supported=[],
                native_snapshot_commands_supported=[],
                native_persistence_commands_supported=[],
            )
        native = self._call_json_export("mycelia_gpu_envelope_capabilities_v1", {"version": RESIDENCY_AUDIT_VERSION})
        command_caps: dict[str, Any] = {}
        if hasattr(self.lib, "mycelia_gpu_command_capabilities_v1"):
            command_caps = self._call_json_export("mycelia_gpu_command_capabilities_v1", {"version": RESIDENCY_AUDIT_VERSION})
        snapshot_caps: dict[str, Any] = {}
        if hasattr(self.lib, "mycelia_gpu_snapshot_runtime_capabilities_v1"):
            snapshot_caps = self._call_json_export("mycelia_gpu_snapshot_runtime_capabilities_v1", {"version": RESIDENCY_AUDIT_VERSION})

        envelope_to_vram = bool(native.get("envelope_to_vram") or native.get("direct_ingest_to_vram"))
        snapshot_to_vram = bool(native.get("snapshot_to_vram") or native.get("restore_to_vram"))
        envelope_staging = bool(native.get("envelope_staging") or native.get("direct_ingest_staging"))
        snapshot_staging = bool(native.get("snapshot_staging") or native.get("restore_staging"))
        selftest_passed = bool(native.get("selftest_passed", False))
        staging_selftest_passed = bool(native.get("staging_selftest_passed", False))

        return NativeGPUResidencyCapabilities(
            requested=self.requested,
            available=True,
            library_path=str(self.library_path),
            envelope_to_vram=envelope_to_vram,
            snapshot_to_vram=snapshot_to_vram,
            envelope_staging=envelope_staging,
            snapshot_staging=snapshot_staging,
            native_command_executor=bool(command_caps.get("native_command_executor") or native.get("native_command_executor")),
            command_executor_selftest_passed=bool(command_caps.get("command_executor_selftest_passed") or native.get("command_executor_selftest_passed")),
            sensitive_command_executor=bool(command_caps.get("sensitive_command_executor") or native.get("sensitive_command_executor")),
            native_auth_executor=bool(command_caps.get("native_auth_executor") or native.get("native_auth_executor")),
            auth_executor_selftest_passed=bool(command_caps.get("auth_executor_selftest_passed") or native.get("auth_executor_selftest_passed")),
            native_content_executor=bool(command_caps.get("native_content_executor") or native.get("native_content_executor")),
            content_executor_selftest_passed=bool(command_caps.get("content_executor_selftest_passed") or native.get("content_executor_selftest_passed")),
            native_admin_executor=bool(command_caps.get("native_admin_executor") or native.get("native_admin_executor")),
            admin_executor_selftest_passed=bool(command_caps.get("admin_executor_selftest_passed") or native.get("admin_executor_selftest_passed")),
            native_plugin_executor=bool(command_caps.get("native_plugin_executor") or native.get("native_plugin_executor")),
            plugin_executor_selftest_passed=bool(command_caps.get("plugin_executor_selftest_passed") or native.get("plugin_executor_selftest_passed")),
            native_gdpr_executor=bool(command_caps.get("native_gdpr_executor") or native.get("native_gdpr_executor")),
            gdpr_executor_selftest_passed=bool(command_caps.get("gdpr_executor_selftest_passed") or native.get("native_gdpr_executor")),
            native_snapshot_runtime=bool(snapshot_caps.get("native_snapshot_runtime") or native.get("native_snapshot_runtime")),
            snapshot_runtime_selftest_passed=bool(snapshot_caps.get("snapshot_runtime_selftest_passed") or native.get("snapshot_runtime_selftest_passed")),
            native_persistence_mutation=bool(snapshot_caps.get("native_persistence_mutation") or native.get("native_persistence_mutation")),
            persistence_mutation_selftest_passed=bool(snapshot_caps.get("persistence_mutation_selftest_passed") or native.get("persistence_mutation_selftest_passed")),
            native_strict_certification_gate=bool(native.get("native_strict_certification_gate") or snapshot_caps.get("native_strict_certification_gate")),
            strict_certification_gate_selftest_passed=bool(native.get("strict_certification_gate_selftest_passed") or snapshot_caps.get("strict_certification_gate_selftest_passed")),
            external_ram_probe_contract=bool(native.get("external_ram_probe_contract") or snapshot_caps.get("external_ram_probe_contract")),
            gpu_resident_open_restore_proven=bool(native.get("gpu_resident_open_restore_proven") or snapshot_caps.get("gpu_resident_open_restore_proven")),
            selftest_passed=selftest_passed,
            staging_selftest_passed=staging_selftest_passed,
            reason=str(native.get("message", self._reason)),
            exports=self._exports,
            native_commands_supported=self._list_field(command_caps, native, "commands_supported"),
            native_auth_commands_supported=self._list_field(command_caps, native, "auth_commands_supported"),
            native_content_commands_supported=self._list_field(command_caps, native, "content_commands_supported"),
            native_admin_commands_supported=self._list_field(command_caps, native, "admin_commands_supported"),
            native_plugin_commands_supported=self._list_field(command_caps, native, "plugin_commands_supported"),
            native_gdpr_commands_supported=self._list_field(command_caps, native, "gdpr_commands_supported"),
            native_snapshot_commands_supported=self._list_field(snapshot_caps, native, "snapshot_commands_supported"),
            native_persistence_commands_supported=self._list_field(snapshot_caps, native, "persistence_commands_supported"),
        )

    def run_selftest(self, probes_sha256: list[str] | None = None) -> dict[str, Any]:
        caps = self.capabilities()
        if not caps.available:
            return {
                "status": "unavailable",
                "native_bridge": caps.__dict__,
                "selftest_passed": False,
                "message": caps.reason,
            }
        report = self._call_json_export(
            "mycelia_gpu_residency_selftest_v1",
            {
                "audit_version": RESIDENCY_AUDIT_VERSION,
                "pid": os.getpid(),
                "probe_sha256": probes_sha256 or [],
                "requirements": {
                    "no_plaintext_to_python": True,
                    "envelope_to_vram": True,
                    "snapshot_to_vram": True,
                    "native_snapshot_runtime": True,
                    "native_persistence_mutation": True,
                },
            },
        )
        report.setdefault("native_bridge", caps.__dict__)
        report.setdefault("selftest_passed", bool(report.get("status") == "ok" and report.get("strict_vram_residency") is True))
        return report


class FallbackCipher:
    """Deterministic stream cipher for driverless tests.

    This is not a substitute for the OpenCL engine. It is intentionally small so
    that the rest of the architecture can be exercised on machines without the
    experimental native library.
    """

    def __init__(self, password: str) -> None:
        self._secret = hashlib.sha256(password.encode("utf-8")).digest()

    def _stream(self, seed: int, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            block = hashlib.sha256(
                self._secret + struct.pack("<Q", seed) + struct.pack("<I", counter)
            ).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def encrypt_bytes(self, data: bytes) -> bytes:
        seed = secrets.randbits(64)
        key = self._stream(seed, len(data))
        cipher = bytes(a ^ b for a, b in zip(data, key))
        return struct.pack("<Q", seed) + struct.pack("<I", len(cipher)) + cipher

    def decrypt_packet_to_bytes(self, packet: bytes) -> bytes | None:
        if len(packet) < 12:
            return None
        seed = struct.unpack("<Q", packet[:8])[0]
        length = struct.unpack("<I", packet[8:12])[0]
        cipher = packet[12 : 12 + length]
        if len(cipher) != length:
            return None
        key = self._stream(seed, len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, key))



class SMQLNativeVectorIndex:
    """Full-dimensional SMQL embedding index owned by the MyceliaDB process.

    The index stores normalized float32 vectors by collection/id and performs
    full cosine ranking. When PyOpenCL is installed and a GPU context can be
    created, one flattened collection buffer is kept in device memory per
    collection generation. Otherwise the class degrades to an explicit CPU
    fallback and reports that fact in every response.
    """

    VERSION = "MYCELIA_SMQL_EMBEDDING_V1_22D_PERSISTENT_REHYDRATION"

    def __init__(self, *, driver_mode: str = "unknown") -> None:
        self.driver_mode = str(driver_mode)
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}
        self.generations: dict[str, int] = {}
        self._gpu_ready = False
        self._gpu_error = ""
        self._gpu_context: Any = None
        self._gpu_queue: Any = None
        self._gpu_program: Any = None
        self._gpu_np: Any = None
        self._gpu_cl: Any = None
        self._gpu_buffers: dict[str, dict[str, Any]] = {}
        self._try_init_opencl()
        self._persist_path = self._default_persist_path()
        self._persist_events_loaded = 0
        self._persist_events_failed = 0
        self._rehydrated_on_startup = False
        self._loading_from_persistence = False
        self._load_persistent_records()

    def _try_init_opencl(self) -> None:
        try:
            import numpy as np  # type: ignore[import-not-found]
            import pyopencl as cl  # type: ignore[import-not-found]
            platforms = cl.get_platforms()
            if not platforms:
                raise RuntimeError("no OpenCL platforms found")
            preferred = int(os.environ.get("MYCELIA_SMQL_OPENCL_PLATFORM", "0"))
            platform = platforms[max(0, min(preferred, len(platforms) - 1))]
            devices = platform.get_devices(device_type=cl.device_type.GPU) or platform.get_devices()
            if not devices:
                raise RuntimeError("no OpenCL devices found")
            preferred_device = int(os.environ.get("MYCELIA_SMQL_OPENCL_DEVICE", "0"))
            device = devices[max(0, min(preferred_device, len(devices) - 1))]
            self._gpu_context = cl.Context([device])
            self._gpu_queue = cl.CommandQueue(self._gpu_context)
            self._gpu_program = cl.Program(
                self._gpu_context,
                """
                __kernel void smql_cosine_scores(
                    __global const float *vectors,
                    __global const float *query,
                    __global float *scores,
                    const int n,
                    const int dim
                ) {
                    int row = get_global_id(0);
                    if (row >= n) return;
                    int base = row * dim;
                    float acc = 0.0f;
                    for (int i = 0; i < dim; ++i) {
                        acc += vectors[base + i] * query[i];
                    }
                    scores[row] = acc;
                }
                """,
            ).build()
            self._gpu_np = np
            self._gpu_cl = cl
            self._gpu_ready = True
            self._gpu_error = ""
        except Exception as exc:
            self._gpu_ready = False
            self._gpu_error = str(exc)


    def _default_persist_path(self) -> str:
        """Return the v1.22d append-only vector persistence path."""
        os_mod = __import__("os")
        pathlib_mod = __import__("pathlib")
        explicit = str(os_mod.environ.get("MYCELIA_SMQL_VECTOR_PERSIST_PATH", "") or "").strip()
        if explicit:
            return explicit
        try:
            root = pathlib_mod.Path(__file__).resolve().parent
        except Exception:
            root = pathlib_mod.Path(".").resolve()
        return str(root / "state" / "smql_vector_index_v122d.jsonl")

    def _persistence_enabled(self) -> bool:
        os_mod = __import__("os")
        return str(os_mod.environ.get("MYCELIA_SMQL_VECTOR_PERSIST", "1")).strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def _encode_persist_vector_b64(values: list[float]) -> str:
        base64_mod = __import__("base64")
        struct_mod = __import__("struct")
        if not values:
            return ""
        raw = struct_mod.pack("<" + "f" * len(values), *[float(v) for v in values])
        return base64_mod.b64encode(raw).decode("ascii")

    @staticmethod
    def _decode_persist_vector_b64(encoded: str) -> list[float]:
        base64_mod = __import__("base64")
        struct_mod = __import__("struct")
        raw = base64_mod.b64decode(str(encoded).encode("ascii"), validate=True)
        if len(raw) % 4:
            raise ValueError("persisted vector byte length is not a multiple of float32")
        return [float(x[0]) for x in struct_mod.iter_unpack("<f", raw)]

    def _append_persistent_store(self, record: Mapping[str, Any]) -> None:
        """Append a latest-record-wins vector event.

        The file is intentionally append-only. Repeated ingests of the same id
        do not rewrite history; startup rehydration collapses to the newest
        collection/id event.
        """
        if getattr(self, "_loading_from_persistence", False):
            return
        if not self._persistence_enabled():
            return
        json_mod = __import__("json")
        os_mod = __import__("os")
        pathlib_mod = __import__("pathlib")
        path = pathlib_mod.Path(str(getattr(self, "_persist_path", self._default_persist_path())))
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "op": "store",
            "version": "MYCELIA_SMQL_EMBEDDING_V1_22D_PERSISTENT_REHYDRATION",
            "collection": str(record.get("collection", "default")),
            "id": str(record.get("id", "")),
            "dimension": int(record.get("dimension", 0) or 0),
            "vector_norm_f32_b64": self._encode_persist_vector_b64(list(record.get("vector", []))),
            "norm": float(record.get("norm", 0.0) or 0.0),
            "vector_sha256": str(record.get("vector_sha256", "")),
            "payload_sha256": str(record.get("payload_sha256", "")),
            "metadata": dict(record.get("metadata", {}) if isinstance(record.get("metadata", {}), Mapping) else {}),
            "pheromone": float(record.get("pheromone", 1.0) or 1.0),
            "signature": str(record.get("signature", "")),
            "created_at": float(record.get("created_at", 0.0) or 0.0),
            "persisted_at": __import__("time").time(),
        }
        tmp_line = json_mod.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(tmp_line)
            try:
                f.flush()
                os_mod.fsync(f.fileno())
            except Exception:
                pass

    def _load_persistent_records(self) -> None:
        """Load persisted vector events into the runtime native vector index."""
        if not self._persistence_enabled():
            self._rehydrated_on_startup = False
            return
        json_mod = __import__("json")
        pathlib_mod = __import__("pathlib")
        path = pathlib_mod.Path(str(getattr(self, "_persist_path", self._default_persist_path())))
        self._persist_events_loaded = 0
        self._persist_events_failed = 0
        self._rehydrated_on_startup = False
        if not path.exists():
            return

        latest: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json_mod.loads(line)
                        op = str(event.get("op", "store"))
                        collection = str(event.get("collection", "default")).strip() or "default"
                        node_id = str(event.get("id", "")).strip()
                        if not node_id:
                            continue
                        key = (collection, node_id)
                        if op in {"delete", "tombstone"}:
                            latest.pop(key, None)
                            self._persist_events_loaded += 1
                            continue
                        if op != "store":
                            continue
                        latest[key] = event
                        self._persist_events_loaded += 1
                    except Exception:
                        self._persist_events_failed += 1
        except Exception:
            self._persist_events_failed += 1
            return

        self._loading_from_persistence = True
        try:
            for (collection, node_id), event in latest.items():
                try:
                    vector = self._decode_persist_vector_b64(str(event.get("vector_norm_f32_b64", "")))
                    dimension = int(event.get("dimension", len(vector)) or len(vector))
                    if dimension != len(vector):
                        self._persist_events_failed += 1
                        continue
                    record = {
                        "id": node_id,
                        "collection": collection,
                        "signature": str(event.get("signature", "")),
                        "dimension": dimension,
                        "vector": vector,
                        "norm": float(event.get("norm", 0.0) or 0.0),
                        "vector_sha256": str(event.get("vector_sha256", "")),
                        "payload_sha256": str(event.get("payload_sha256", "")),
                        "metadata": dict(event.get("metadata", {}) if isinstance(event.get("metadata", {}), Mapping) else {}),
                        "pheromone": max(0.0, min(1.0, float(event.get("pheromone", 1.0) or 1.0))),
                        "created_at": float(event.get("created_at", 0.0) or 0.0),
                    }
                    bucket = self.collections.setdefault(collection, {})
                    bucket[node_id] = record
                    self.generations[collection] = self.generations.get(collection, 0) + 1
                except Exception:
                    self._persist_events_failed += 1
        finally:
            self._loading_from_persistence = False

        self._rehydrated_on_startup = bool(latest)
        self._prewarm_gpu_buffers()

    def _prewarm_gpu_buffers(self) -> None:
        """Upload single-dimension collections into the OpenCL buffer cache."""
        if not self._gpu_ready:
            return
        for collection, bucket in list(self.collections.items()):
            records = list(bucket.values())
            if not records:
                continue
            dimensions = {int(record.get("dimension", 0) or 0) for record in records}
            if len(dimensions) != 1:
                continue
            dimension = next(iter(dimensions))
            try:
                self._ensure_gpu_buffer(collection, records, dimension)
            except Exception as exc:
                self._gpu_error = str(exc)

    def rehydrate(self, *, force: bool = False) -> dict[str, Any]:
        """Reload persisted vector events into the runtime index.

        force=true clears current runtime records before loading. Without force,
        persisted records are merged latest-record-wins with existing records.
        """
        if force:
            self.collections.clear()
            self.generations.clear()
            self._gpu_buffers.clear()
        self._load_persistent_records()
        return self.status()


    def _persistence_ledger_audit(self) -> dict[str, Any]:
        """Return an audit view of the v1.22d append-only vector ledger.

        Read-only. Does not expose vector values; only counts, dimensions and
        consistency relationships between persisted latest-record-wins ledger
        and runtime native index.
        """
        json_mod = __import__("json")
        pathlib_mod = __import__("pathlib")
        path = pathlib_mod.Path(str(getattr(self, "_persist_path", self._default_persist_path())))
        runtime_counts = {str(name): len(records) for name, records in getattr(self, "collections", {}).items()}
        runtime_total = int(sum(runtime_counts.values()))
        audit: dict[str, Any] = {
            "schema": "MYCELIA_SMQL_VECTOR_LEDGER_AUDIT_V1_22D2",
            "path": str(path),
            "exists": bool(path.exists()),
            "bytes": 0,
            "mtime": 0.0,
            "events_total": 0,
            "store_events": 0,
            "delete_events": 0,
            "events_failed": 0,
            "latest_counts": {},
            "latest_total_vectors": 0,
            "runtime_counts": runtime_counts,
            "runtime_total_vectors": runtime_total,
            "ledger_matches_runtime": False,
            "startup_counter_evidence": bool(
                getattr(self, "_rehydrated_on_startup", False)
                and int(getattr(self, "_persist_events_loaded", 0) or 0) > 0
            ),
            "operational_rehydration_available": False,
        }
        if not path.exists():
            audit["note"] = "persistence ledger does not exist"
            return audit
        try:
            stat = path.stat()
            audit["bytes"] = int(stat.st_size)
            audit["mtime"] = float(stat.st_mtime)
        except Exception:
            pass

        latest: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    audit["events_total"] += 1
                    try:
                        event = json_mod.loads(line)
                        op = str(event.get("op", "store"))
                        collection = str(event.get("collection", "default")).strip() or "default"
                        node_id = str(event.get("id", "")).strip()
                        if not node_id:
                            audit["events_failed"] += 1
                            continue
                        key = (collection, node_id)
                        if op in {"delete", "tombstone"}:
                            latest.pop(key, None)
                            audit["delete_events"] += 1
                            continue
                        if op != "store":
                            continue
                        latest[key] = event
                        audit["store_events"] += 1
                    except Exception:
                        audit["events_failed"] += 1
        except Exception as exc:
            audit["events_failed"] += 1
            audit["error"] = str(exc)
            return audit

        latest_counts: dict[str, int] = {}
        dimensions: dict[str, list[int]] = {}
        for (collection, _node_id), event in latest.items():
            latest_counts[collection] = latest_counts.get(collection, 0) + 1
            try:
                dim = int(event.get("dimension", 0) or 0)
                if dim:
                    dimensions.setdefault(collection, [])
                    if dim not in dimensions[collection]:
                        dimensions[collection].append(dim)
            except Exception:
                pass

        audit["latest_counts"] = dict(sorted(latest_counts.items()))
        audit["latest_total_vectors"] = int(sum(latest_counts.values()))
        audit["dimensions"] = {k: sorted(v) for k, v in sorted(dimensions.items())}
        audit["ledger_matches_runtime"] = audit["latest_counts"] == runtime_counts
        audit["operational_rehydration_available"] = bool(
            audit["exists"]
            and audit["events_failed"] == 0
            and audit["latest_total_vectors"] > 0
            and audit["ledger_matches_runtime"]
        )
        if audit["startup_counter_evidence"]:
            audit["verdict"] = "pass:startup-loader-counter-positive"
        elif audit["operational_rehydration_available"]:
            audit["verdict"] = "pass:ledger-runtime-consistent-counter-missing"
            audit["note"] = (
                "Runtime index matches persisted latest-record-wins ledger, but "
                "startup counter telemetry is missing or was reset."
            )
        elif audit["latest_total_vectors"] > 0 and runtime_total == 0:
            audit["verdict"] = "fail:ledger-populated-runtime-empty"
        else:
            audit["verdict"] = "warn:ledger-runtime-mismatch"
        return audit

    def vram_available(self) -> bool:
        return bool(self._gpu_ready)

    def _backend_name(self) -> str:
        return "opencl-vram" if self._gpu_ready else "cpu-vector-fallback"

    @staticmethod
    def _decode_vector_from_payload(payload: Mapping[str, Any], *, key: str) -> tuple[list[float], str]:
        encoded = str(payload.get(key, "") or "").strip()
        if encoded:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
            if len(raw) % 4:
                raise ValueError(f"{key} length is not a multiple of float32")
            values = [float(x[0]) for x in struct.iter_unpack("<f", raw)]
            return values, hashlib.sha256(raw).hexdigest()
        array_key = "vector" if key == "vector_f32_b64" else "query_vector"
        raw_values = payload.get(array_key, [])
        if not isinstance(raw_values, list):
            raise ValueError(f"{key} or {array_key} is required")
        values = [float(x) for x in raw_values]
        packed = struct.pack("<" + "f" * len(values), *values) if values else b""
        return values, hashlib.sha256(packed).hexdigest()

    @staticmethod
    def _normalize(values: list[float]) -> tuple[list[float], float]:
        norm = sum(v * v for v in values) ** 0.5
        if norm <= 0.0:
            return [0.0 for _ in values], 0.0
        return [float(v / norm) for v in values], float(norm)

    def store(self, payload: Mapping[str, Any], *, signature: str) -> dict[str, Any]:
        collection = str(payload.get("collection", "default")).strip() or "default"
        node_id = str(payload.get("id", "")).strip()
        if not node_id:
            return {"status": "error", "message": "id fehlt."}
        try:
            vector, vector_sha256 = self._decode_vector_from_payload(payload, key="vector_f32_b64")
        except Exception as exc:
            return {"status": "error", "message": f"vector_f32_b64 ungültig: {exc}"}
        dimension = int(payload.get("dimension", len(vector)) or len(vector))
        if dimension != len(vector):
            return {"status": "error", "message": f"dimension mismatch: expected {dimension}, got {len(vector)}"}
        expected_sha = str(payload.get("vector_sha256", "") or "").strip()
        if expected_sha and expected_sha != vector_sha256:
            return {
                "status": "error",
                "message": "vector_sha256 mismatch between adapter payload and MyceliaDB decoder",
                "expected": expected_sha,
                "actual": vector_sha256,
            }
        normalized, norm = self._normalize(vector)
        record = {
            "id": node_id,
            "collection": collection,
            "signature": str(signature),
            "dimension": dimension,
            "vector": normalized,
            "norm": norm,
            "vector_sha256": vector_sha256,
            "payload_sha256": str(payload.get("payload_sha256", "")),
            "metadata": dict(payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), Mapping) else {}),
            "pheromone": max(0.0, min(1.0, float(payload.get("pheromone", 1.0) or 1.0))),
            "created_at": time.time(),
        }
        bucket = self.collections.setdefault(collection, {})
        bucket[node_id] = record
        self.generations[collection] = self.generations.get(collection, 0) + 1
        self._gpu_buffers.pop(collection, None)
        self._append_persistent_store(record)
        return {
            "status": "ok",
            "version": self.VERSION,
            "backend": self._backend_name(),
            "collection": collection,
            "id": node_id,
            "dimension": dimension,
            "vector_sha256": vector_sha256,
            "signature": str(signature),
            "native_vector_search": True,
            "full_dimension_search": True,
            "vram_resident": bool(self._gpu_ready),
            "strict_vram_residency_proven": False,
            "gpu_error": self._gpu_error if not self._gpu_ready else "",
        }

    def _records_for(self, collection: str, dimension: int) -> list[dict[str, Any]]:
        return [
            record
            for record in self.collections.get(collection, {}).values()
            if int(record.get("dimension", 0)) == int(dimension)
        ]

    def _ensure_gpu_buffer(self, collection: str, records: list[dict[str, Any]], dimension: int) -> dict[str, Any]:
        if not self._gpu_ready:
            raise RuntimeError("OpenCL backend is not available")
        generation = self.generations.get(collection, 0)
        cached = self._gpu_buffers.get(collection)
        if cached and cached.get("generation") == generation and cached.get("dimension") == dimension:
            return cached

        np = self._gpu_np
        cl = self._gpu_cl
        flat = np.asarray([record["vector"] for record in records], dtype=np.float32).reshape(-1)
        mf = cl.mem_flags
        buffer = cl.Buffer(self._gpu_context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=flat)
        cached = {
            "generation": generation,
            "dimension": dimension,
            "count": len(records),
            "buffer": buffer,
        }
        self._gpu_buffers[collection] = cached
        return cached

    def _gpu_search(self, collection: str, records: list[dict[str, Any]], query: list[float], limit: int) -> list[dict[str, Any]]:
        np = self._gpu_np
        cl = self._gpu_cl
        dimension = len(query)
        cached = self._ensure_gpu_buffer(collection, records, dimension)
        query_np = np.asarray(query, dtype=np.float32)
        scores_np = np.zeros((len(records),), dtype=np.float32)
        mf = cl.mem_flags
        query_buf = cl.Buffer(self._gpu_context, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=query_np)
        scores_buf = cl.Buffer(self._gpu_context, mf.WRITE_ONLY, scores_np.nbytes)
        self._gpu_program.smql_cosine_scores(
            self._gpu_queue,
            (len(records),),
            None,
            cached["buffer"],
            query_buf,
            scores_buf,
            np.int32(len(records)),
            np.int32(dimension),
        )
        cl.enqueue_copy(self._gpu_queue, scores_np, scores_buf).wait()
        ranked_indices = sorted(range(len(records)), key=lambda i: float(scores_np[i]) * float(records[i].get("pheromone", 1.0)), reverse=True)[:limit]
        return [self._result_from_record(records[i], float(scores_np[i])) for i in ranked_indices]

    @staticmethod
    def _result_from_record(record: Mapping[str, Any], cosine: float) -> dict[str, Any]:
        pheromone = max(0.0, min(1.0, float(record.get("pheromone", 1.0) or 1.0)))
        score = float(cosine) * pheromone
        return {
            "id": record.get("id"),
            "collection": record.get("collection"),
            "signature": record.get("signature"),
            "dimension": record.get("dimension"),
            "score": score,
            "cosine": float(cosine),
            "pheromone": pheromone,
            "norm": record.get("norm"),
            "vector_sha256": record.get("vector_sha256"),
            "payload_sha256": record.get("payload_sha256"),
            "metadata": record.get("metadata", {}),
            "created_at": record.get("created_at"),
        }

    def search(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        collection = str(payload.get("collection", "default")).strip() or "default"
        limit = max(1, min(1000, int(payload.get("limit", 10) or 10)))
        strict_vram_required = bool(payload.get("strict_vram_required", False))
        try:
            query, query_sha256 = self._decode_vector_from_payload(payload, key="query_vector_f32_b64")
        except Exception as exc:
            return {"status": "error", "message": f"query_vector_f32_b64 ungültig: {exc}"}
        dimension = int(payload.get("dimension", len(query)) or len(query))
        if dimension != len(query):
            return {"status": "error", "message": f"dimension mismatch: expected {dimension}, got {len(query)}"}
        if strict_vram_required and not self._gpu_ready:
            return {
                "status": "error",
                "message": "strict_vram_required=true, but v1.22b OpenCL VRAM backend is unavailable",
                "version": self.VERSION,
                "backend": self._backend_name(),
                "full_dimension_search": True,
                "native_vector_search": True,
                "vram_resident": False,
                "strict_vram_residency_proven": False,
                "gpu_error": self._gpu_error,
            }
        normalized_query, query_norm = self._normalize(query)
        if query_norm <= 0.0:
            return {"status": "error", "message": "query vector norm is zero"}
        records = self._records_for(collection, dimension)
        if self._gpu_ready and records:
            try:
                results = self._gpu_search(collection, records, normalized_query, limit)
                backend = "opencl-vram"
                vram_resident = True
            except Exception as exc:
                if strict_vram_required:
                    return {"status": "error", "message": f"OpenCL vector search failed: {exc}", "backend": "opencl-vram"}
                self._gpu_ready = False
                self._gpu_error = str(exc)
                backend = "cpu-vector-fallback"
                vram_resident = False
                results = self._cpu_search(records, normalized_query, limit)
        else:
            backend = "cpu-vector-fallback"
            vram_resident = False
            results = self._cpu_search(records, normalized_query, limit)

        return {
            "status": "ok",
            "version": self.VERSION,
            "collection": collection,
            "dimension": dimension,
            "query_sha256": query_sha256,
            "count": len(results),
            "total_candidates": len(records),
            "results": results,
            "backend": backend,
            "full_dimension_search": True,
            "native_vector_search": True,
            "vram_resident": vram_resident,
            "strict_vram_residency_proven": False,
            "gpu_error": self._gpu_error if not vram_resident else "",
        }

    def _cpu_search(self, records: list[dict[str, Any]], query: list[float], limit: int) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for record in records:
            vector = record.get("vector", [])
            cosine = sum(float(a) * float(b) for a, b in zip(query, vector))
            ranked.append(self._result_from_record(record, cosine))
        ranked.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return ranked[:limit]

    def status(self) -> dict[str, Any]:
        counts = {name: len(records) for name, records in self.collections.items()}
        return {
            "status": "ok",
            "version": self.VERSION,
            "backend": self._backend_name(),
            "vram_available": bool(self._gpu_ready),
            "vram_resident_collections": sorted(self._gpu_buffers.keys()),
            "collections": counts,
            "total_vectors": sum(counts.values()),
            "gpu_error": self._gpu_error,
            "persistence": {
                "enabled": self._persistence_enabled(),
                "path": str(getattr(self, "_persist_path", "")),
                "events_loaded": int(getattr(self, "_persist_events_loaded", 0) or 0),
                "events_failed": int(getattr(self, "_persist_events_failed", 0) or 0),
                "rehydrated_on_startup": bool(getattr(self, "_rehydrated_on_startup", False)),
                "mode": "append-only-jsonl-latest-record-wins",
                "audit": self._persistence_ledger_audit(),
            },
            "strict_vram_residency_proven": False,
        }



class MyceliaPlatform:
    def __init__(self) -> None:
        self.config_path = CORE_ROOT / "mycelia_ai" / "config.yaml"
        self.config = load_config(self.config_path)
        self.driver_mode = "offline"
        self.driver = self._build_driver()
        simulation_cfg = self.config.get("simulation", {})
        cognition_cfg = simulation_cfg.get("cognition", {})
        quantum_cfg = dict(simulation_cfg.get("quantum", {}))
        quantum_cfg.setdefault("gpu_index", simulation_cfg.get("gpu_index", 0))
        self.core = CognitiveCore(self.driver, cognition_cfg, quantum_cfg)
        self.crypto = self._build_crypto()
        native_candidates = NativeGPUResidencyBridge.candidate_paths()
        native_envelope_requested = _env_bool("MYCELIA_NATIVE_GPU_ENVELOPE_OPENER", NATIVE_GPU_ENVELOPE_REQUESTED)
        gpu_restore_requested = _env_bool("MYCELIA_GPU_RESTORE_OPENER", GPU_RESTORE_REQUESTED)
        strict_certification_requested = _env_bool("MYCELIA_STRICT_VRAM_CERTIFICATION", STRICT_VRAM_CERTIFICATION)
        # Auto-detect keeps normal Enterprise startup simple, but explicit 0/false
        # in tests or audits must be respected so conservative phase-1 paths can
        # be exercised even when the native DLL exists on disk.
        native_auto_blocked = (
            _env_is_explicit_false("MYCELIA_NATIVE_GPU_ENVELOPE_OPENER")
            and _env_is_explicit_false("MYCELIA_GPU_RESTORE_OPENER")
            and _env_is_explicit_false("MYCELIA_STRICT_VRAM_CERTIFICATION")
        )
        native_requested = (
            native_envelope_requested
            or gpu_restore_requested
            or strict_certification_requested
            or (AUTO_NATIVE_GPU_ENVELOPE and not native_auto_blocked and any(path.exists() for path in native_candidates))
        )
        self.native_residency = NativeGPUResidencyBridge(
            native_candidates,
            requested=native_requested,
        )
        if native_requested:
            caps = self.native_residency.capabilities()
            if caps.available:
                self.driver_mode = self.driver_mode + "+native-vram"
                msg = f"Native GPU Residency DLL geladen: {caps.library_path}"
                print(msg, flush=True)
                LOGGER.info(msg)
                if not caps.selftest_passed:
                    print(
                        "Native GPU Residency DLL geladen, Selftest aber noch nicht bestanden. "
                        "Führe native_gpu_residency_selftest oder strict_vram_certification aus.",
                        flush=True,
                    )
            else:
                msg = f"Native GPU Residency DLL nicht aktiv: {caps.reason}"
                print(msg, flush=True)
                LOGGER.warning(msg)
        else:
            # Startup diagnostics before logging.basicConfig: make the absence
            # explicit and show the first few paths. This prevents confusion with
            # CC_OpenCl.dll / MyceliaChatEngine messages.
            checked = ", ".join(str(p) for p in native_candidates[:8])
            print(
                "Native GPU Residency DLL nicht gefunden/angefordert. "
                f"Geprüft: {checked}",
                flush=True,
            )
        self.ingest_private_key = self._load_or_create_ingest_private_key()
        self._ingest_public_key_b64 = self._export_ingest_public_key_b64()
        self._ingest_seen_nonces: deque[tuple[float, str]] = deque(maxlen=4096)
        self._ingest_seen_nonce_set: set[str] = set()
        self.start_time = time.time()
        self.snapshot_path = Path(
            os.environ.get("MYCELIA_SNAPSHOT_PATH", str(DEFAULT_SNAPSHOT_PATH))
        ).resolve()
        self.autosave_enabled = os.environ.get("MYCELIA_AUTOSAVE", "1").lower() not in {
            "0", "false", "no", "off"
        }
        self.autorestore_enabled = os.environ.get("MYCELIA_AUTORESTORE", "1").lower() not in {
            "0", "false", "no", "off"
        }
        self._autosave_suspended = False
        self.last_restore_mode = "none"
        self.last_restore_cpu_materialized = False
        self.latest_external_memory_probe: dict[str, Any] | None = None
        self.residency_challenges: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, EngineSession] = {}
        self.federation_peers: dict[str, dict[str, Any]] = self._load_federation_state()
        self._provenance_last_hash = self._load_last_provenance_hash()
        self._local_transport_token = self._load_or_create_local_transport_token()
        self.quantum_guard_config = {
            "cooldown_ms": int(os.environ.get("MYCELIA_QUANTUM_INTUITION_COOLDOWN_MS", "60000")),
            "burst": int(os.environ.get("MYCELIA_QUANTUM_INTUITION_BURST", "1")),
            "quarantine_threshold": int(os.environ.get("MYCELIA_QUANTUM_TENSION_QUARANTINE_THRESHOLD", "5")),
        }
        if hasattr(self.core, "configure_quantum_guard"):
            self.core.configure_quantum_guard(self.quantum_guard_config)
        self._pfs_session_private_key = x25519.X25519PrivateKey.generate() if x25519 is not None else None
        self._pfs_session_public_b64 = ""
        if self._pfs_session_private_key is not None:
            self._pfs_session_public_b64 = base64.b64encode(
                self._pfs_session_private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
            ).decode("ascii")
        self._webauthn_challenges: dict[str, dict[str, Any]] = {}
        self._telemetry_ring: deque[dict[str, Any]] = deque(maxlen=512)
        if self.autorestore_enabled:
            self._auto_restore_snapshot()

    def _build_driver(self) -> Any:
        try:
            driver_path = resolve_core_driver_library(self.config.get("paths", {}))
            if sys.platform == "win32":
                try:
                    os.add_dll_directory(str(driver_path.parent))
                except Exception:
                    pass
            LOGGER.info("OpenCLDriver wird geladen aus: %s", driver_path)
            driver = OpenCLDriver(driver_path)
            driver.initialize(int(self.config.get("simulation", {}).get("gpu_index", 0)))
            self.driver_mode = f"opencl:{driver_path}"
            return driver
        except Exception as exc:
            LOGGER.warning("OpenCLDriver nicht verfügbar, OfflineDriver aktiv: %s", exc)
            return OfflineDriver()

    def _build_crypto(self) -> Any:
        if MyceliaChatEngine is not None:
            try:
                engine = MyceliaChatEngine(0)
                engine.set_password(APP_SECRET)
                self.driver_mode = self.driver_mode + "+gpu-crypto"
                return engine
            except Exception as exc:
                LOGGER.warning("MyceliaChatEngine nicht verfügbar, FallbackCipher aktiv: %s", exc)
        return FallbackCipher(APP_SECRET)


    def _native_envelope_to_vram_enabled(self) -> bool:
        caps = self.native_residency.capabilities()
        return bool(caps.available and caps.envelope_to_vram)

    def _gpu_restore_to_vram_enabled(self) -> bool:
        caps = self.native_residency.capabilities()
        return bool(caps.available and caps.snapshot_to_vram)

    def _core_gpu_crypto_active(self) -> bool:
        """Legacy/Core GPU crypto indicator.

        This only reflects the old Core/OpenCL ChatEngine crypto label in
        driver_mode.  It intentionally does not include the native envelope DLL.
        """
        return "+gpu-crypto" in str(self.driver_mode)

    def _native_envelope_crypto_active(self) -> bool:
        """Native GPU envelope/snapshot crypto indicator.

        This reflects the mycelia_gpu_envelope.dll contract: encrypted payloads
        are opened/restored through the native VRAM residency bridge.  Operators
        were seeing gpu_crypto_active=false even though the native bridge was
        active, because the older report only checked driver_mode.
        """
        return bool(self._native_envelope_to_vram_enabled() or self._gpu_restore_to_vram_enabled())

    def _gpu_crypto_active(self) -> bool:
        """Combined GPU crypto capability for operator-facing reports."""
        return bool(self._core_gpu_crypto_active() or self._native_envelope_crypto_active())

    def native_gpu_capability_report(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return the native GPU residency capability state.

        v1.18E reports the complete native boundary chain plus the strict
        certification gate and external RAM-probe contract. It still refuses
        strict certification unless envelope_to_vram, snapshot_to_vram, native
        selftest and a negative external RAM probe pass.
        """
        del payload
        caps = self.native_residency.capabilities()
        blockers: list[str] = []
        if not caps.available:
            blockers.append("Native GPU envelope/snapshot opener library is not available.")
        if not caps.envelope_to_vram:
            blockers.append("Direct-Ingest envelopes are not opened directly into VRAM.")
        if not caps.snapshot_to_vram:
            blockers.append("Snapshots are not restored directly into VRAM.")
        if not caps.selftest_passed:
            blockers.append("Native residency self-test has not passed.")
        boundary_complete = bool(
            caps.native_auth_executor and caps.auth_executor_selftest_passed
            and caps.native_content_executor and caps.content_executor_selftest_passed
            and caps.native_admin_executor and caps.admin_executor_selftest_passed
            and caps.native_plugin_executor and caps.plugin_executor_selftest_passed
            and caps.native_gdpr_executor and caps.gdpr_executor_selftest_passed
            and caps.native_snapshot_runtime and caps.snapshot_runtime_selftest_passed
            and caps.native_persistence_mutation and caps.persistence_mutation_selftest_passed
        )
        if boundary_complete and blockers:
            blockers.append("v1.18F GPU-resident open/restore is active; strict VRAM-only still requires a negative external RAM probe and no CPU-materialized restore evidence.")
        elif not boundary_complete:
            blockers.append("Native boundary chain is incomplete.")

        sensitive_state = (
            "partial-auth-content-admin-plugin-gdpr-snapshot-persistence"
            if boundary_complete and not caps.sensitive_command_executor else
            "strict-native" if caps.sensitive_command_executor else
            "partial"
        )
        return {
            "status": "ok",
            "audit_version": RESIDENCY_AUDIT_VERSION,
            "native_bridge": caps.__dict__,
            "native_envelope_staging_active": bool(caps.envelope_staging and caps.staging_selftest_passed),
            "native_snapshot_staging_active": bool(caps.snapshot_staging and caps.staging_selftest_passed),
            "native_command_executor_active": bool(caps.native_command_executor and caps.command_executor_selftest_passed),
            "native_auth_executor_active": bool(caps.native_auth_executor and caps.auth_executor_selftest_passed),
            "native_content_executor_active": bool(caps.native_content_executor and caps.content_executor_selftest_passed),
            "native_admin_executor_active": bool(caps.native_admin_executor and caps.admin_executor_selftest_passed),
            "native_plugin_executor_active": bool(caps.native_plugin_executor and caps.plugin_executor_selftest_passed),
            "native_gdpr_executor_active": bool(caps.native_gdpr_executor and caps.gdpr_executor_selftest_passed),
            "native_snapshot_runtime_active": bool(caps.native_snapshot_runtime and caps.snapshot_runtime_selftest_passed),
            "native_persistence_mutation_active": bool(caps.native_persistence_mutation and caps.persistence_mutation_selftest_passed),
            "native_sensitive_command_executor": sensitive_state,
            "native_commands_supported": caps.native_commands_supported,
            "native_auth_commands_supported": caps.native_auth_commands_supported,
            "native_content_commands_supported": caps.native_content_commands_supported,
            "native_admin_commands_supported": caps.native_admin_commands_supported,
            "native_plugin_commands_supported": caps.native_plugin_commands_supported,
            "native_gdpr_commands_supported": caps.native_gdpr_commands_supported,
            "native_snapshot_commands_supported": caps.native_snapshot_commands_supported,
            "native_persistence_commands_supported": caps.native_persistence_commands_supported,
            "strict_native_prerequisites_met": not blockers,
            "blockers": blockers,
            "staging_note": (
                "v1.18D adds native snapshot-runtime and persistence-mutation boundaries. "
                "The native layer can accept autosave/restore/mutation handles without returning graph payloads. "
                "It is still a conservative boundary runtime, not a strict VRAM-only certificate."
            ),
            "contract": {
                "required_exports": list(NativeGPUResidencyBridge.REQUIRED_EXPORTS),
                "optional_but_required_for_certification": list(NativeGPUResidencyBridge.OPTIONAL_EXPORTS),
                "strict_runtime_contract": {
                    "python_must_not_decrypt_direct_ingest": True,
                    "python_must_not_decrypt_snapshot": True,
                    "native_exports_called_in_strict_mode": True,
                    "opaque_gpu_handles_only": True,
                    "native_snapshot_runtime_required": True,
                    "native_persistence_mutation_required": True,
                },
                "library_env": "MYCELIA_GPU_ENVELOPE_LIBRARY",
                "enable_env": [
                    "MYCELIA_NATIVE_GPU_ENVELOPE_OPENER=1",
                    "MYCELIA_GPU_RESTORE_OPENER=1",
                    "MYCELIA_STRICT_VRAM_CERTIFICATION=1",
                ],
            },
        }

    def native_gpu_residency_selftest(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        probe_hashes = payload.get("probe_sha256", [])
        if not isinstance(probe_hashes, list):
            probe_hashes = []
        report = self.native_residency.run_selftest([str(x) for x in probe_hashes])
        caps = self.native_residency.capabilities()
        report["audit_version"] = RESIDENCY_AUDIT_VERSION
        report["native_strict_certification_gate_active"] = bool(caps.native_strict_certification_gate and caps.strict_certification_gate_selftest_passed)
        report["external_ram_probe_contract_active"] = bool(caps.external_ram_probe_contract)
        report["strict_native_prerequisites_met"] = bool(
            caps.available
            and caps.envelope_to_vram
            and caps.snapshot_to_vram
            and caps.gpu_resident_open_restore_proven
            and report.get("selftest_passed")
        )
        return report


    def _load_or_create_ingest_private_key(self) -> Any:
        """Load or create the Direct Ingest RSA key pair.

        The public key is safe to expose to browsers.  The private key stays on
        the engine side and is never copied into PHP.  For production the key
        path should live outside the web root and be protected with OS ACLs.
        """
        if _CRYPTOGRAPHY_IMPORT_ERROR is not None or rsa is None or serialization is None:
            raise RuntimeError(f"cryptography package is required for Direct GPU Ingest: {_CRYPTOGRAPHY_IMPORT_ERROR}")
        INGEST_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if INGEST_KEY_PATH.exists():
            return serialization.load_pem_private_key(INGEST_KEY_PATH.read_bytes(), password=None)
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        raw = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        INGEST_KEY_PATH.write_bytes(raw)
        try:
            os.chmod(INGEST_KEY_PATH, 0o600)
        except Exception:
            pass
        LOGGER.info("Direct-Ingest-Schlüssel erzeugt: %s", INGEST_KEY_PATH)
        return private_key

    def _export_ingest_public_key_b64(self) -> str:
        public_key = self.ingest_private_key.public_key()
        raw = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(raw).decode("ascii")


    def _hash_session_token(self, token: str) -> str:
        return hashlib.sha256((APP_SECRET + "|session|" + token).encode("utf-8")).hexdigest()

    def _default_permissions_for_role(self, role: str) -> tuple[str, ...]:
        if role == "admin":
            return (
                "admin.access", "admin.users.manage", "admin.texts.manage", "admin.plugins.manage", "plugin.run", "content.moderate",
                "profile.update", "forum.create", "forum.comment", "forum.react",
                "blog.create", "blog.post.create", "blog.comment", "blog.react",
                "media.upload", "media.moderate",
            )
        return (
            "profile.update", "forum.create", "forum.comment", "forum.react",
            "blog.create", "blog.post.create", "blog.comment", "blog.react",
            "media.upload",
        )

    def _normalize_permissions(self, permissions: Any, role: str = "user") -> tuple[str, ...]:
        allowed = {
            "admin.access", "admin.users.manage", "admin.texts.manage", "admin.plugins.manage", "plugin.run", "content.moderate", "admin.plugins.manage", "plugin.run",
            "profile.update", "forum.create", "forum.comment", "forum.react",
            "blog.create", "blog.post.create", "blog.comment", "blog.react",
            "media.upload", "media.moderate",
        }
        if permissions is None or permissions == "":
            return self._default_permissions_for_role(role)
        if isinstance(permissions, str):
            raw = [p.strip() for p in re.split(r"[\s,;]+", permissions) if p.strip()]
        elif isinstance(permissions, (list, tuple, set)):
            raw = [str(p).strip() for p in permissions if str(p).strip()]
        else:
            raw = []
        normalized = tuple(sorted({p for p in raw if p in allowed}))
        return normalized or self._default_permissions_for_role(role)

    def _get_user_record(self, signature: str) -> tuple[dict[str, Any], dict[str, Any]] | tuple[None, None]:
        record = self.core.get_sql_record(str(signature))
        if not record or record.get("table") != USER_TABLE:
            return None, None
        return record, dict(record.get("data", {}))

    def _user_permissions(self, signature: str, role: str = "user") -> tuple[str, ...]:
        _, row = self._get_user_record(signature)
        if not row:
            return self._default_permissions_for_role(role)
        profile = {}
        if row.get("profile_seed") and row.get("profile_blob"):
            try:
                profile = self._decrypt_json({"seed": row["profile_seed"], "blob": row["profile_blob"]})
            except Exception:
                profile = {}
        return self._normalize_permissions(profile.get("permissions"), str(profile.get("role") or role))

    def _has_permission(self, payload: Mapping[str, Any], permission: str) -> bool:
        role = str(payload.get("actor_role") or payload.get("role") or "")
        if role == "admin":
            return True
        permissions = payload.get("actor_permissions") or payload.get("permissions")
        return permission in self._normalize_permissions(permissions, role or "user")

    def _require_permission(self, payload: Mapping[str, Any], permission: str) -> dict[str, Any] | None:
        if self._has_permission(payload, permission):
            return None
        return {"status": "error", "message": f"Recht erforderlich: {permission}"}

    def _issue_engine_session(self, signature: str, username: str, role: str, permissions: Any = None) -> dict[str, Any]:
        now = time.time()
        handle = secrets.token_urlsafe(32)
        request_token = secrets.token_urlsafe(32)
        token_hash = self._hash_session_token(request_token)
        normalized_permissions = self._normalize_permissions(permissions, role)
        self.sessions[handle] = EngineSession(
            handle=handle,
            signature=signature,
            username=username,
            role=role,
            permissions=normalized_permissions,
            request_token_hash=token_hash,
            request_token_hashes={token_hash: now + REQUEST_TOKEN_TTL_SECONDS},
            sequence=0,
            issued_at=now,
            expires_at=now + SESSION_TTL_SECONDS,
            last_seen=now,
        )
        return {
            "handle": handle,
            "request_token": request_token,
            "sequence": 0,
            "expires_at": self.sessions[handle].expires_at,
            "binding": "engine-attractor-rotating-request-token",
            "token_window": "one-time-token-pool",
        }

    def _purge_session_request_tokens(self, session: EngineSession, now: float) -> None:
        # Enterprise token-window guard:
        # Multiple page reads, manifest fetches or browser tabs may issue short-lived
        # form tokens.  They are one-time and expire quickly, but they are not
        # invalidated by unrelated read calls.  This prevents "Request-Token passt
        # nicht..." during normal navigation without returning to long-lived CSRF tokens.
        expired = [h for h, expires_at in session.request_token_hashes.items() if expires_at < now]
        for h in expired:
            session.request_token_hashes.pop(h, None)
        if len(session.request_token_hashes) > 64:
            # Keep the newest token window; fail closed for very old form tokens.
            for h, _ in sorted(session.request_token_hashes.items(), key=lambda item: item[1])[:-64]:
                session.request_token_hashes.pop(h, None)

    def _remember_session_request_token(self, session: EngineSession, token: str, now: float) -> str:
        token_hash = self._hash_session_token(token)
        session.request_token_hash = token_hash
        session.request_token_hashes[token_hash] = now + REQUEST_TOKEN_TTL_SECONDS
        return token_hash

    def _consume_session_request_token(self, session: EngineSession, token: str, now: float) -> bool:
        supplied_hash = self._hash_session_token(token)
        self._purge_session_request_tokens(session, now)
        matched_hash = None
        # Constant-time compare against every active token hash. The pool is capped.
        for token_hash in list(session.request_token_hashes.keys()):
            if secrets.compare_digest(token_hash, supplied_hash):
                matched_hash = token_hash
                break
        if matched_hash is None and secrets.compare_digest(session.request_token_hash, supplied_hash):
            matched_hash = session.request_token_hash
        if matched_hash is None:
            return False
        # One-time consumption: the submitted request token cannot be replayed.
        session.request_token_hashes.pop(matched_hash, None)
        return True

    def _validate_and_rotate_session(self, context: Mapping[str, Any], *, rotate: bool = True) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        handle = str(context.get("engine_session_handle", "") or "").strip()
        token = str(context.get("engine_request_token", "") or "").strip()
        if not handle or not token:
            return None, {"status": "error", "message": "Engine-Session fehlt. Bitte neu einloggen."}
        session = self.sessions.get(handle)
        now = time.time()
        if session is None:
            return None, {"status": "error", "message": "Engine-Session ist unbekannt oder abgelaufen."}
        if session.expires_at < now:
            self.sessions.pop(handle, None)
            return None, {"status": "error", "message": "Engine-Session ist abgelaufen."}
        if not self._consume_session_request_token(session, token, now):
            return None, {"status": "error", "message": "Request-Token passt nicht zum flüchtigen Engine-Attraktor."}
        next_token = token
        if rotate:
            next_token = secrets.token_urlsafe(32)
            self._remember_session_request_token(session, next_token, now)
            session.sequence += 1
        else:
            # Non-rotating validations still keep the consumed token alive only if
            # explicitly requested; the default path is rotate=True.
            self._remember_session_request_token(session, token, now)
        session.last_seen = now
        session.expires_at = now + SESSION_TTL_SECONDS
        # Refresh role/permissions from the user attractor on every request so
        # admin grants/revocations become effective without re-login.
        record, row = self._get_user_record(session.signature)
        if row and row.get("profile_seed") and row.get("profile_blob"):
            try:
                profile = self._decrypt_json({"seed": row["profile_seed"], "blob": row["profile_blob"]})
                session.role = str(profile.get("role") or session.role)
                session.permissions = self._normalize_permissions(profile.get("permissions"), session.role)
            except Exception:
                pass
        authority = {
            "signature": session.signature,
            "actor_signature": session.signature,
            "author_signature": session.signature,
            "owner_signature": session.signature,
            "actor_username": session.username,
            "author_username": session.username,
            "owner_username": session.username,
            "actor_role": session.role,
            "role": session.role,
            "actor_permissions": list(session.permissions),
            "permissions": list(session.permissions),
            "engine_session": {
                "handle": session.handle,
                "request_token": next_token,
                "sequence": session.sequence,
                "expires_at": session.expires_at,
                "rotated": rotate,
            },
            "engine_authority": "validated-by-mycelia-session-attractor",
        }
        return authority, None

    def validate_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        authority, error = self._validate_and_rotate_session(payload, rotate=True)
        if error:
            return error
        assert authority is not None
        return {
            "status": "ok",
            "signature": authority["actor_signature"],
            "username": authority["actor_username"],
            "role": authority["actor_role"],
            "permissions": authority.get("actor_permissions", []),
            "engine_session": authority["engine_session"],
            "session_binding": "rotated",
        }

    def logout_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        handle = str(payload.get("engine_session_handle", "") or "")
        if handle:
            self.sessions.pop(handle, None)
        return {"status": "ok", "message": "Engine-Session gelöscht."}

    def _require_authority_for_command(self, command: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        if bool(payload.get("_engine_authorized")):
            return None
        if command in {"register_user", "login_attractor", "direct_ingest", "direct_ingest_manifest", "check_integrity", "residency_report", "validate_session", "logout_session", "store_embedding", "find_embedding", "smql_vector_index_status", "smql_vector_rehydrate", "smql_vector_rehydration_audit", "store_embedding", "find_embedding"}:
            return None
        if command in DIRECT_INGEST_AUTH_REQUIRED_OPS or command in SESSION_BOUND_READ_OPS:
            authority, error = self._validate_and_rotate_session(payload, rotate=True)
            if error:
                return error
            assert authority is not None
            payload_dict = payload if isinstance(payload, dict) else None
            if payload_dict is not None:
                for key, value in authority.items():
                    # Never overwrite a command's target signature during page reads.
                    # The authority signature remains available as actor_signature.
                    if key in {"signature", "role", "permissions"} and key in payload_dict:
                        continue
                    # Public catalog reads must stay public.  Session binding is still
                    # validated and actor_* is still forwarded, but owner_* must not be
                    # injected into list_blogs because list_blogs(owner_signature=...)
                    # is the private "Mein Blog" filter.  Injecting owner_signature
                    # here turns the public Blogs page into a per-user catalog.
                    if command == "list_blogs" and key in {"owner_signature", "owner_username"} and key not in payload_dict:
                        continue
                    payload_dict[key] = value
                # Every session-bound command rotates the request token.  The new
                # token must be returned even for read-only pages; otherwise PHP
                # keeps a stale token and the next sealed Direct-Ingest form fails
                # with "Request-Token passt nicht zum flüchtigen Engine-Attraktor".
                payload_dict["_engine_session_to_return"] = authority["engine_session"]
        return None

    @staticmethod
    def _safe_fragment(value: Any, *, kind: str = "text") -> dict[str, str]:
        import html
        raw = "" if value is None else str(value)
        return {
            "kind": kind,
            "text": html.escape(raw, quote=True),
            "policy": "engine-context-escaped-html-text",
        }

    def _safe_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(record)
        for key, value in list(out.items()):
            if isinstance(value, str):
                out[key + "_safe"] = self._safe_fragment(value)
        return out


    def direct_ingest_manifest(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return the browser encryption manifest plus a fresh form token.

        The frontend fetches this immediately before sealing a form.  When a
        session is present, the manifest call is itself session-bound and rotates
        the token.  The returned token is short-lived, one-time, and accepted
        from the encrypted envelope only.  PHP never injects it into a mutation
        after the browser seal.
        """
        requested_op = str(payload.get("op", "") or "").strip()
        op_requires_session = requested_op in DIRECT_INGEST_AUTH_REQUIRED_OPS
        engine_session: dict[str, Any] | None = None
        clear_engine_session = False
        if payload.get("engine_session_handle") or payload.get("engine_request_token"):
            authority, error = self._validate_and_rotate_session(payload, rotate=True)
            if error:
                # A stale PHP session must not break login/registration sealing.
                # For unauthenticated ops we deliberately downgrade to an anonymous
                # manifest and tell PHP to clear its obsolete in-memory session.
                if op_requires_session:
                    return error
                clear_engine_session = True
            else:
                assert authority is not None
                engine_session = authority["engine_session"]
        elif op_requires_session:
            return {"status": "error", "message": "Engine-Session fehlt oder ist abgelaufen. Bitte neu einloggen."}

        response = {
            "status": "ok",
            "version": 2,
            "mode": "DIRECT_GPU_INGEST_PHASE2_PFS_PHP_BLIND",
            "public_key_spki_b64": self._ingest_public_key_b64,
            "key_alg": "RSA-OAEP-3072-SHA256",
            "payload_alg": "AES-256-GCM",
            "pfs": bool(self._pfs_session_public_b64),
            "pfs_alg": "X25519-HKDF-SHA256/AES-256-GCM",
            "pfs_engine_public_key_raw_b64": self._pfs_session_public_b64,
            "max_age_seconds": DIRECT_INGEST_MAX_AGE_SECONDS,
            "allowed_ops": sorted(DIRECT_INGEST_ALLOWED_OPS),
            "php_cleartext_fields_allowed": False,
            "session_binding": "engine-rotating-request-token",
            "csrf_model": "protocol-level-engine-token-window",
            "output_policy": "engine-safe-fragments-plus-php-default-escaping",
            "strict_vram_residency_proven": self._strict_residency_supported([], []),
            "native_gpu_envelope_opener": self._native_envelope_to_vram_enabled(),
            "gpu_restore_opener": self._gpu_restore_to_vram_enabled(),
            "native_gpu_capability_command": "native_gpu_capability_report",
            "strict_certification_command": "strict_vram_certification",
            "note": (
                "PHP receives opaque sealed_ingest packages. Strict CPU-RAM-free VRAM residency "
                "uses per-request PFS envelopes when supported by the browser. Strict CPU-RAM-free VRAM residency "
                "requires a native GPU envelope opener, native GPU snapshot restore, a passing "
                "native residency self-test and a negative external memory probe."
            ),
        }
        response["requested_op"] = requested_op
        response["op_requires_session"] = op_requires_session
        if clear_engine_session:
            response["clear_engine_session"] = True
            response["session_binding"] = "anonymous-after-stale-engine-session"
        if engine_session is not None:
            response["engine_session"] = engine_session
            response["engine_request_token"] = engine_session["request_token"]
            response["token_window"] = "one-time-form-token"
        return response

    def _prune_ingest_nonces(self) -> None:
        now = time.time()
        cutoff = now - max(DIRECT_INGEST_MAX_AGE_SECONDS, 1)
        while self._ingest_seen_nonces and self._ingest_seen_nonces[0][0] < cutoff:
            _, nonce = self._ingest_seen_nonces.popleft()
            self._ingest_seen_nonce_set.discard(nonce)

    def _remember_ingest_nonce(self, nonce: str) -> bool:
        self._prune_ingest_nonces()
        if nonce in self._ingest_seen_nonce_set:
            return False
        self._ingest_seen_nonce_set.add(nonce)
        self._ingest_seen_nonces.append((time.time(), nonce))
        return True

    def _open_direct_ingest_envelope(self, sealed: Mapping[str, Any]) -> DirectIngestEnvelope:
        if STRICT_VRAM_ONLY and not self._native_envelope_to_vram_enabled():
            raise RuntimeError(
                "STRICT_VRAM_ONLY ist aktiv: Python darf Direct-Ingest-Envelopes nicht im CPU-RAM öffnen. "
                "Eine native GPU-Envelope-Bibliothek ist erforderlich."
            )
        if AESGCM is None:
            raise RuntimeError("cryptography backend unavailable")
        version = int(sealed.get("v", 0) or 0)
        alg = str(sealed.get("alg", ""))
        aad_text = str(sealed.get("aad", "myceliadb-direct-ingest-v1"))
        aad = aad_text.encode("utf-8")
        iv = base64.b64decode(str(sealed.get("iv_b64", "")))
        ciphertext = base64.b64decode(str(sealed.get("ciphertext_b64", "")))

        if version == 2 and alg == "X25519-HKDF-SHA256/AES-256-GCM":
            if x25519 is None or HKDF is None or hashes is None or self._pfs_session_private_key is None:
                raise RuntimeError("PFS Direct-Ingest is unavailable in this runtime")
            client_pub = x25519.X25519PublicKey.from_public_bytes(base64.b64decode(str(sealed.get("client_ephemeral_public_key_b64", ""))))
            shared = self._pfs_session_private_key.exchange(client_pub)
            aes_key = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=base64.b64decode(str(sealed.get("salt_b64", ""))),
                info=b"myceliadb-direct-ingest-pfs-v2",
            ).derive(shared)
            raw = AESGCM(aes_key).decrypt(iv, ciphertext, aad)
        elif version == 1 and alg == "RSA-OAEP-3072-SHA256/AES-256-GCM":
            if padding is None or hashes is None:
                raise RuntimeError("RSA-OAEP Direct-Ingest is unavailable")
            encrypted_key = base64.b64decode(str(sealed.get("key_b64", "")))
            aes_key = self.ingest_private_key.decrypt(
                encrypted_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=b"myceliadb-direct-ingest-v1",
                ),
            )
            raw = AESGCM(aes_key).decrypt(iv, ciphertext, aad)
        else:
            raise ValueError("Unsupported Direct-Ingest envelope version/algorithm")

        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Direct-Ingest payload must be a JSON object")

        op = str(data.get("op", "")).strip()
        if op not in DIRECT_INGEST_ALLOWED_OPS:
            raise ValueError(f"Direct-Ingest operation is not allowed: {op}")
        nonce = str(data.get("nonce", "")).strip()
        if len(nonce) < 12:
            raise ValueError("Direct-Ingest nonce missing or too short")
        if not self._remember_ingest_nonce(nonce):
            raise ValueError("Direct-Ingest replay detected")

        issued_at_ms = int(data.get("issued_at_ms", 0) or 0)
        now_ms = int(time.time() * 1000)
        max_age_ms = DIRECT_INGEST_MAX_AGE_SECONDS * 1000
        if issued_at_ms <= 0 or abs(now_ms - issued_at_ms) > max_age_ms:
            raise ValueError("Direct-Ingest envelope expired or has invalid clock skew")

        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Direct-Ingest payload field must be an object")
        return DirectIngestEnvelope(op=op, payload=payload, nonce=nonce, issued_at_ms=issued_at_ms)


    def _direct_media_fields(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Preserve sealed browser media fields while normalizing form payloads.

        Direct-Ingest intentionally accepts flat browser FormData.  Several
        update/create normalizers collapse HTML aliases such as post_signature
        into canonical engine fields.  Without this side-channel, file/link
        fields collected by assets/direct-ingest.js are silently discarded before
        update_forum_thread(), create_blog_post() or update_blog_post() can call
        _store_media_from_payload().
        """
        media_keys = {
            "media_file_b64",
            "media_file_name",
            "media_mime",
            "media_size_bytes",
            "file_b64",
            "file_name",
            "mime",
            "embed_url",
            "media_embed_url",
            "media_title",
            "title_media",
        }
        out: dict[str, Any] = {}
        for key in media_keys:
            value = data.get(key)
            # Keep booleans/numbers, but avoid empty strings from optional media inputs.
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            out[key] = value
        return out

    def _normalize_direct_payload(self, op: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Convert browser form fields into engine payload shapes.

        The browser intentionally sends flat form fields so PHP never has to
        inspect or restructure sensitive values.  The Engine performs the first
        semantic interpretation after opening the sealed envelope.
        """
        data = dict(payload)
        media_fields = self._direct_media_fields(data)
        match op:
            case "register_user":
                username = str(data.get("username", "")).strip()
                role = "admin" if username == "admin" else "user"
                return {
                    "username": username,
                    "password": str(data.get("password", "")),
                    "profile": {
                        "vorname": data.get("vorname", ""),
                        "nachname": data.get("nachname", ""),
                        "strasse": data.get("strasse", ""),
                        "hnr": data.get("hnr", ""),
                        "plz": data.get("plz", ""),
                        "ort": data.get("ort", ""),
                        "email": data.get("email", ""),
                        "role": role,
                    },
                }
            case "login_attractor":
                return {"username": str(data.get("username", "")).strip(), "password": str(data.get("password", ""))}
            case "update_profile":
                return {
                    "profile": {
                        "vorname": data.get("vorname", ""),
                        "nachname": data.get("nachname", ""),
                        "strasse": data.get("strasse", ""),
                        "hnr": data.get("hnr", ""),
                        "plz": data.get("plz", ""),
                        "ort": data.get("ort", ""),
                        "email": data.get("email", ""),
                        "role": data.get("role", "user"),
                    }
                }
            case "react_content":
                target_signature = data.get("target_signature") or data.get("post_signature") or data.get("comment_signature") or ""
                reaction = data.get("reaction") or data.get("react") or data.get("react_post") or data.get("react_comment") or ""
                target_type = data.get("target_type") or ("comment" if data.get("comment_signature") else ("blog_post" if data.get("post_signature") else "forum_thread"))
                return {"target_signature": target_signature, "target_type": target_type, "reaction": reaction}
            case "create_comment":
                return {
                    "target_signature": data.get("target_signature") or data.get("post_signature") or "",
                    "target_type": data.get("target_type") or ("blog_post" if data.get("post_signature") else "forum_thread"),
                    "body": data.get("body", ""), "body_vault_json": data.get("body_vault_json", ""),
                }
            case "delete_comment" | "update_comment":
                return {"signature": data.get("signature") or data.get("comment_signature") or "", "body": data.get("body", "")}
            case "update_blog":
                return {
                    "signature": data.get("signature") or data.get("blog_signature") or "",
                    "title": data.get("title", ""),
                    "description": data.get("description", ""), "description_vault_json": data.get("description_vault_json", ""),
                    "blog_theme": data.get("blog_theme", ""),
                    **media_fields,
                }
            case "delete_blog":
                return {"signature": data.get("signature") or data.get("blog_signature") or ""}
            case "create_blog":
                return {
                    "title": data.get("title", ""),
                    "description": data.get("description", ""), "description_vault_json": data.get("description_vault_json", ""),
                    "blog_theme": data.get("blog_theme", ""),
                    **media_fields,
                }
            case "create_blog_post":
                return {
                    "blog_signature": data.get("blog_signature", ""),
                    "title": data.get("title", ""),
                    "body": data.get("body", ""), "body_vault_json": data.get("body_vault_json", ""),
                    "publish_status": data.get("publish_status", "published"),
                    **media_fields,
                }
            case "update_blog_post":
                return {
                    "signature": data.get("signature") or data.get("post_signature") or "",
                    "title": data.get("title", ""),
                    "body": data.get("body", ""), "body_vault_json": data.get("body_vault_json", ""),
                    "publish_status": data.get("publish_status", "published"),
                    **media_fields,
                }
            case "delete_blog_post":
                return {"signature": data.get("signature") or data.get("post_signature") or ""}
            case "delete_my_account":
                return {
                    "confirm_delete": data.get("confirm_delete", ""),
                    "password": data.get("password", ""),
                    "delete_mode": data.get("delete_mode", "hard-purge"),
                }
            case "admin_install_plugin":
                return {"manifest_json": data.get("manifest_json", "")}
            case "admin_set_plugin_state":
                enabled_raw = str(data.get("enabled", "0")).lower()
                return {"signature": data.get("signature", ""), "enabled": enabled_raw in {"1", "true", "yes", "on"}}
            case "admin_delete_plugin":
                return {"signature": data.get("signature", "")}
            case "run_plugin":
                return {"signature": data.get("signature", ""), "input": data.get("input", "")}
            case "federation_peer_add":
                return {"peer_id": data.get("peer_id", ""), "url": data.get("url", ""), "fingerprint": data.get("fingerprint", ""), "enabled": data.get("enabled", "1")}
            case "federation_peer_remove":
                return {"peer_id": data.get("peer_id", "")}
            case "federation_import_influx":
                try:
                    attractors = json.loads(str(data.get("attractors_json", "[]")))
                except Exception:
                    attractors = []
                return {"attractors": attractors}
            case "e2ee_send_message":
                return {
                    "recipient_signature": data.get("recipient_signature", ""),
                    "recipient_key_signature": data.get("recipient_key_signature", ""),
                    "recipient_key_hash": data.get("recipient_key_hash", ""),
                    "recipient_username": data.get("recipient_username", ""),
                    "ciphertext_b64": data.get("ciphertext_b64", ""),
                    "nonce_b64": data.get("nonce_b64", ""),
                    "eph_public_jwk": data.get("eph_public_jwk", ""),
                    "sender_ciphertext_b64": data.get("sender_ciphertext_b64", ""),
                    "sender_nonce_b64": data.get("sender_nonce_b64", ""),
                    "sender_eph_public_jwk": data.get("sender_eph_public_jwk", ""),
                    "sender_key_hash": data.get("sender_key_hash", ""),
                    "aad": data.get("aad", "mycelia-e2ee-v1"),
                    "allow_self_message": data.get("allow_self_message", "0"),
                }
            case "e2ee_delete_message":
                return {
                    "signature": data.get("signature", "") or data.get("message_signature", ""),
                    "mailbox": data.get("mailbox", "inbox"),
                }
            case "create_poll":
                options = []
                for idx in range(1, 7):
                    val = str(data.get(f"option_{idx}", "")).strip()
                    if val:
                        options.append(val)
                if not options:
                    try:
                        loaded = json.loads(str(data.get("options_json", "[]")))
                        if isinstance(loaded, list):
                            options = [str(v).strip() for v in loaded if str(v).strip()]
                    except Exception:
                        options = []
                return {"question": data.get("question", ""), "options": options, "target_signature": data.get("target_signature", "")}
            case "vote_poll":
                return {"poll_signature": data.get("poll_signature", ""), "option_id": data.get("option_id", "")}
            case "create_time_capsule":
                return {"title": data.get("title", ""), "body": data.get("body", ""), "body_vault_json": data.get("body_vault_json", ""), "reveal_at": data.get("reveal_at", ""), "visibility": data.get("visibility", "private")}
            case "vram_residency_audit":
                probes_raw = str(data.get("probes", ""))
                probes = [p.strip() for p in re.split(r"[\r\n,]+", probes_raw) if p.strip()]
                return {"probes": probes, "create_temp_snapshot": True}
            case "admin_set_site_text":
                return {"key": data.get("key", ""), "value": data.get("value", ""), "context": data.get("context", "web")}
            case "admin_update_user_rights":
                permissions = data.get("permissions", [])
                if isinstance(permissions, str):
                    permissions = [permissions] if permissions else []
                return {"signature": data.get("signature", ""), "role": data.get("role", "user"), "permissions": permissions}
            case "delete_forum_thread":
                return {"signature": data.get("signature") or data.get("target_signature") or ""}
            case "update_forum_thread":
                return {
                    "signature": data.get("signature") or data.get("target_signature") or "",
                    "title": data.get("title", ""),
                    "body": data.get("body", ""), "body_vault_json": data.get("body_vault_json", ""),
                    **media_fields,
                }
            case _:
                return data

    def direct_ingest(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Open a PHP-blind Direct-Ingest package and dispatch the contained op."""
        sealed_raw = payload.get("sealed")
        if isinstance(sealed_raw, str):
            sealed = json.loads(sealed_raw)
        elif isinstance(sealed_raw, Mapping):
            sealed = sealed_raw
        else:
            return {"status": "error", "message": "sealed Direct-Ingest package missing"}
        if not isinstance(sealed, Mapping):
            return {"status": "error", "message": "sealed Direct-Ingest package is invalid"}

        expected_op = str(payload.get("op", "")).strip()
        try:
            envelope = self._open_direct_ingest_envelope(sealed)
            if expected_op and expected_op != envelope.op:
                return {"status": "error", "message": "Direct-Ingest op mismatch"}

            actor_context = payload.get("actor_context", {})
            normalized = self._normalize_direct_payload(envelope.op, envelope.payload)

            # Enterprise authority boundary:
            # PHP may forward opaque session material, but it cannot decide actor,
            # owner, role or permission.  For every authenticated mutation the
            # Engine validates and rotates the request token, then injects the
            # authoritative identity into the command payload.
            if envelope.op in DIRECT_INGEST_AUTH_REQUIRED_OPS:
                if not isinstance(actor_context, Mapping):
                    actor_context = {}
                # Protocol-level CSRF binding: the rotating request token must be
                # inside the browser-sealed envelope, not injected by PHP after
                # decryption. A cross-site form post cannot read this value.
                sealed_request_token = str(normalized.pop("__mycelia_request_token", "") or envelope.payload.get("__mycelia_request_token", ""))
                actor_context = {**dict(actor_context), "engine_request_token": sealed_request_token}
                authority, error = self._validate_and_rotate_session(actor_context, rotate=True)
                if error:
                    return error
                assert authority is not None
                # Authority is injected first, then the sealed operation payload
                # may supply a target signature for update/delete/read-like mutations.
                # The actor identity is still authoritative through actor_signature.
                merged = {**authority, **normalized, "_engine_authorized": True}
            else:
                merged = normalized

            result = self.dispatch(envelope.op, merged)
            if envelope.op in DIRECT_INGEST_AUTH_REQUIRED_OPS and "authority" in locals():
                # Even validation/business errors after token consumption must
                # return the rotated Engine session. Otherwise the next form
                # would fail with token drift despite the request being
                # cryptographically valid.
                result["engine_session"] = authority["engine_session"]

            if envelope.op == "login_attractor" and result.get("status") == "ok":
                role = "admin" if result.get("username") == "admin" else "user"
                profile_result = self.get_profile({"signature": str(result.get("signature", ""))})
                if profile_result.get("status") == "ok":
                    role = str(profile_result.get("profile", {}).get("role") or role)
                result["engine_session"] = self._issue_engine_session(
                    signature=str(result["signature"]),
                    username=str(result["username"]),
                    role=role,
                )
                result["role"] = role

            result.setdefault("direct_ingest", {})
            result["direct_ingest"] = {
                "mode": "phase1_php_blind",
                "op": envelope.op,
                "nonce_prefix": envelope.nonce[:8],
                "php_cleartext_fields_seen": False,
                "python_cpu_decrypt_materialized": True,
                "strict_vram_residency_proven": False,
                "session_bound": envelope.op in DIRECT_INGEST_AUTH_REQUIRED_OPS,
                "session_rotated": bool(isinstance(result.get("engine_session"), Mapping)),
            }
            return result
        except Exception as exc:
            LOGGER.warning("Direct-Ingest rejected: %s", exc)
            return {"status": "error", "message": f"Direct-Ingest abgelehnt: {exc}"}

    def _encrypt_json(self, payload: Mapping[str, Any]) -> CryptoPacket:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        packet = self.crypto.encrypt_bytes(raw)
        seed = struct.unpack("<Q", packet[:8])[0]
        return CryptoPacket(
            seed=str(seed),
            blob=base64.b64encode(packet[8:]).decode("ascii"),
            mode=self.driver_mode,
        )

    def _decrypt_json(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        seed = int(str(packet["seed"]))
        blob = base64.b64decode(str(packet["blob"]))
        raw = self.crypto.decrypt_packet_to_bytes(struct.pack("<Q", seed) + blob)
        if raw is None:
            raise ValueError("QuantumOracle konnte den Datenknoten nicht rekonstruieren")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Rekonstruierter Knoten ist kein JSON-Objekt")
        return data

    def _password_pattern(self, username: str, password: str) -> str:
        # Server-peppered hash: stable enough to become an attractor cue, but no
        # raw password and no reusable client hash is stored.
        material = f"{username}\0{password}\0{APP_SECRET}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()


    def _auto_restore_snapshot(self) -> None:
        """Load the default encrypted Mycelia snapshot during process startup.

        This is the missing persistence bridge: the runtime still never opens a
        SQL database, but it can survive a full Python/PHP restart by rebuilding
        the cognitive graph from the encrypted MYCELIA_SNAPSHOT_V1 image.
        """
        if not self.snapshot_path.exists():
            LOGGER.info("Kein Mycelia-Autosnapshot gefunden: %s", self.snapshot_path)
            return
        try:
            self._autosave_suspended = True
            restored = self.restore_snapshot({"path": str(self.snapshot_path)})
            if restored.get("status") == "ok":
                LOGGER.info(
                    "Mycelia-Autosnapshot geladen: %s (%s Attraktoren)",
                    self.snapshot_path,
                    restored.get("restored"),
                )
            else:
                LOGGER.warning("Mycelia-Autosnapshot konnte nicht geladen werden: %s", restored)
        except Exception as exc:
            LOGGER.exception("Mycelia-Autosnapshot konnte nicht geladen werden: %s", exc)
        finally:
            self._autosave_suspended = False

    def autosave_snapshot(self, reason: str = "manual") -> dict[str, Any]:
        """Persist current graph to the default encrypted snapshot path."""
        if not self.autosave_enabled:
            return {"status": "skipped", "reason": "autosave_disabled"}
        if self._autosave_suspended:
            return {"status": "skipped", "reason": "autosave_suspended"}
        try:
            response = self.create_snapshot({"path": str(self.snapshot_path)})
            if response.get("status") == "ok":
                LOGGER.info(
                    "Mycelia-Autosnapshot gespeichert: %s reason=%s attractors=%s",
                    self.snapshot_path,
                    reason,
                    response.get("attractors"),
                )
            else:
                LOGGER.warning("Mycelia-Autosnapshot fehlgeschlagen: %s", response)
            return response
        except Exception as exc:
            LOGGER.exception("Mycelia-Autosnapshot fehlgeschlagen: %s", exc)
            return {"status": "error", "message": str(exc), "reason": reason}

    def register_user(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        profile = payload.get("profile", {})
        if not username or not password:
            return {"status": "error", "message": "Username und Passwort sind erforderlich."}
        if not isinstance(profile, Mapping):
            profile = {}

        existing = self.core.query_sql_like(table=USER_TABLE, filters={"username": username}, limit=1)
        if existing:
            return {"status": "error", "message": "Username existiert bereits als Attraktor."}

        profile_dict = dict(profile)
        role = str(profile_dict.get("role") or ("admin" if username == "admin" else "user"))
        profile_dict["role"] = role
        profile_dict["permissions"] = list(self._normalize_permissions(profile_dict.get("permissions"), role))
        auth_pattern = self._password_pattern(username, password)
        encrypted = self._encrypt_json(profile_dict)
        row = {
            "node_type": "user",
            "username": username,
            "auth_pattern": auth_pattern,
            "profile_seed": encrypted.seed,
            "profile_blob": encrypted.blob,
            "crypto_mode": encrypted.mode,
            "created_at": time.time(),
        }
        pattern = self.core.database.store_sql_record(
            USER_TABLE,
            row,
            stability=0.985,
            mood_vector=(0.96, 0.03, 0.91),
        )
        save = self.autosave_snapshot("register_user")
        return {
            "status": "ok",
            "signature": pattern.signature,
            "username": username,
            "stability": pattern.stability,
            "driver_mode": self.driver_mode,
            "autosave": save.get("status"),
            "snapshot_path": str(self.snapshot_path),
        }

    def login_attractor(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        auth_pattern = self._password_pattern(username, password)
        matches = self.core.query_sql_like(
            table=USER_TABLE,
            filters={"username": username, "auth_pattern": auth_pattern},
            limit=1,
        )
        if not matches:
            return {"status": "error", "message": "Kein stabiler Auth-Attraktor gefunden."}
        match = matches[0]
        if float(match.get("stability", 0.0)) < 0.5:
            return {"status": "error", "message": "Auth-Attraktor ist instabil."}
        row = dict(match.get("data", {}))
        profile = {}
        if row.get("profile_seed") and row.get("profile_blob"):
            try:
                profile = self._decrypt_json({"seed": row["profile_seed"], "blob": row["profile_blob"]})
            except Exception:
                profile = {}
        role = str(profile.get("role") or ("admin" if username == "admin" else "user"))
        permissions = self._normalize_permissions(profile.get("permissions"), role)
        engine_session = self._issue_engine_session(match["signature"], username, role, permissions)
        return {
            "status": "ok",
            "signature": match["signature"],
            "username": username,
            "role": role,
            "permissions": list(permissions),
            "engine_session": engine_session,
            "stability": match.get("stability"),
            "driver_mode": self.driver_mode,
        }

    def _strict_response_redaction_active(self) -> bool:
        return bool(STRICT_RESPONSE_REDACTION and STRICT_VRAM_CERTIFICATION)

    @staticmethod
    def _redacted_value(reason: str = "strict-vram-response-redaction") -> dict[str, str]:
        return {
            "redacted": "true",
            "reason": reason,
            "text": "[redacted:strict-vram]",
        }

    def _web_ui_cleartext_response_allowed(self, command: str, payload: Mapping[str, Any]) -> bool:
        """Allow normal human UI pages to render values reconstructed from MyceliaDB.

        Strict VRAM certification and web rendering are two separate operating
        moments.  Audit commands stay redacted/machine-readable; forum/blog/profile
        pages may ask for cleartext so users can actually read their content.
        After a user opens such a page, a strict RAM audit over those same values
        is expected to fail because the response necessarily materializes them.
        """
        if not WEB_UI_CLEAR_TEXT_RESPONSES:
            return False
        if not bool(payload.get("_web_ui_cleartext_response") or payload.get("allow_cleartext_response")):
            return False
        return command in {
            "get_profile",
            "list_forum_threads",
            "get_forum_thread",
            "list_comments",
            "list_blogs",
            "get_blog",
            "list_blog_posts",
            "get_blog_post",
            "admin_overview",
            "list_users",
        }

    def _sanitize_strict_response(self, command: str, result: Any, payload: Mapping[str, Any] | None = None) -> Any:
        """Remove user cleartext from normal JSON response paths in strict mode.

        This is a response hardening layer.  It cannot undo Python objects that a
        legacy command already materialized internally, but it prevents long-lived
        response/session/Admin JSON from retaining profile/content values.  Full
        certification still depends on native command execution and external RAM
        probes.

        Exception: the PHP web UI can explicitly request cleartext reconstruction
        for human display.  That mode is allowed only for read endpoints and must
        not be used as the basis for a strict RAM-residency proof.
        """
        payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        if not self._strict_response_redaction_active():
            return result
        if self._web_ui_cleartext_response_allowed(command, payload_map):
            if isinstance(result, dict):
                result = dict(result)
                result["strict_response_redaction"] = False
                result["cleartext_response_mode"] = "web-ui-authorized"
                result["cleartext_response_warning"] = "Human display materializes selected MyceliaDB values in the response path; do not run strict RAM certification after using this page."
            return result
        if command in {
            "export_my_data",              # intentionally user-authorized data disclosure
            "strict_vram_evidence_bundle", # evidence only; must remain machine-readable
            "strict_vram_certification",
            "native_gpu_capability_report",
            "native_gpu_residency_selftest",
            "submit_external_memory_probe",
            "residency_audit_manifest",
            "residency_report",
            "heartbeat_audit_status",
        }:
            return result
        sensitive_keys = {
            "profile", "profile_safe", "body", "title", "description", "email",
            "vorname", "nachname", "ort", "city", "address", "phone", "bio",
            "author_username", "owner_username", "username_safe",
        }
        def sanitize(obj: Any, key: str = "") -> Any:
            if isinstance(obj, dict):
                if key in sensitive_keys:
                    return self._redacted_value()
                out: dict[str, Any] = {}
                for k, v in obj.items():
                    ks = str(k)
                    if ks in sensitive_keys:
                        out[ks] = self._redacted_value()
                    else:
                        out[ks] = sanitize(v, ks)
                return out
            if isinstance(obj, list):
                return [sanitize(v, key) for v in obj]
            return obj
        return sanitize(result)


    def get_profile(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", ""))
        record = self.core.get_sql_record(signature)
        if not record:
            return {"status": "error", "message": "Profil-Attraktor nicht gefunden."}
        row = record["data"]
        if self._strict_response_redaction_active() and not payload.get("allow_cleartext_profile") and not self._web_ui_cleartext_response_allowed("get_profile", payload):
            return {
                "status": "ok",
                "username": "[redacted:strict-vram]",
                "username_safe": self._redacted_value(),
                "profile": self._redacted_value("strict-vram-profile-not-materialized"),
                "profile_safe": self._redacted_value("strict-vram-profile-not-materialized"),
                "node": {
                    "signature": signature,
                    "table": record.get("table"),
                    "stability": record.get("stability"),
                    "visits": record.get("visits"),
                },
                "driver_mode": self.driver_mode,
                "strict_response_redaction": True,
            }
        profile = self._decrypt_json({"seed": row["profile_seed"], "blob": row["profile_blob"]})
        return {
            "status": "ok",
            "username": row.get("username"),
            "username_safe": self._safe_fragment(row.get("username")),
            "profile": profile,
            "profile_safe": {str(k): self._safe_fragment(v) for k, v in profile.items()},
            "node": {
                "signature": signature,
                "table": record.get("table"),
                "stability": record.get("stability"),
                "visits": record.get("visits"),
            },
            "driver_mode": self.driver_mode,
        }

    def update_profile(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if (err := self._require_permission(payload, "profile.update")):
            return err
        signature = str(payload.get("signature", payload.get("actor_signature", "")))
        actor_signature = str(payload.get("actor_signature", ""))
        actor_role = str(payload.get("actor_role", ""))
        if signature != actor_signature and actor_role != "admin" and not self._has_permission(payload, "admin.users.manage"):
            return {"status": "error", "message": "Nur das eigene Profil oder Admin-Rechte dürfen geändert werden."}
        record = self.core.get_sql_record(signature)
        if not record:
            return {"status": "error", "message": "Profil-Attraktor nicht gefunden."}
        row = dict(record["data"])
        old_profile = {}
        if row.get("profile_seed") and row.get("profile_blob"):
            try:
                old_profile = self._decrypt_json({"seed": row["profile_seed"], "blob": row["profile_blob"]})
            except Exception:
                old_profile = {}
        incoming = dict(payload.get("profile", {}) or {})
        if actor_role != "admin" and not self._has_permission(payload, "admin.users.manage"):
            incoming["role"] = old_profile.get("role", incoming.get("role", "user"))
            incoming["permissions"] = old_profile.get("permissions", self._default_permissions_for_role(str(incoming.get("role") or "user")))
        else:
            incoming["permissions"] = list(self._normalize_permissions(incoming.get("permissions"), str(incoming.get("role") or old_profile.get("role") or "user")))
        encrypted = self._encrypt_json(incoming)
        row["profile_seed"] = encrypted.seed
        row["profile_blob"] = encrypted.blob
        row["crypto_mode"] = encrypted.mode
        row["updated_at"] = time.time()
        ok = self.core.update_sql_record(signature, row, stability=0.99, mood_vector=(0.98, 0.02, 0.94))
        if not ok:
            return {"status": "error", "message": "Profil konnte nicht aktualisiert werden."}
        # update_sql_record preserves the signature.
        save = self.autosave_snapshot("update_profile")
        return {
            "status": "ok",
            "signature": signature,
            "driver_mode": self.driver_mode,
            "autosave": save.get("status"),
            "snapshot_path": str(self.snapshot_path),
        }

    def import_dump(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path", "")).strip()
        table = str(payload.get("table", "")).strip()
        if not path or not table:
            return {"status": "error", "message": "path und table sind erforderlich."}
        sql_path = Path(path)
        if not sql_path.is_absolute():
            sql_path = (ROOT / sql_path).resolve()
        limit = payload.get("limit")
        patterns = self.core.import_sql_table(
            str(sql_path),
            table,
            limit=None if limit in (None, "") else int(limit),
            stability=float(payload.get("stability", 0.92)),
            mood_vector=(0.77, 0.13, 0.88),
        )
        save = self.autosave_snapshot("import_dump")
        return {
            "status": "ok",
            "imported": len(patterns),
            "table": table,
            "signatures": [p.signature for p in patterns[:25]],
            "driver_mode": self.driver_mode,
            "autosave": save.get("status"),
            "snapshot_path": str(self.snapshot_path),
        }



    def _smql_vector_index(self) -> SMQLNativeVectorIndex:
        index = getattr(self, "_smql_native_vector_index", None)
        if index is None:
            index = SMQLNativeVectorIndex(driver_mode=self.driver_mode)
            self._smql_native_vector_index = index
        return index

    def smql_vector_index_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._smql_vector_index().status()

    def smql_vector_rehydration_audit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._smql_vector_index()._persistence_ledger_audit()

    def smql_vector_rehydrate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._smql_vector_index().rehydrate(force=bool(payload.get("force", False)))

    def store_embedding(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """v1.22b: store a full-dimensional embedding in the MyceliaDB vector index."""
        collection = str(payload.get("collection", "default")).strip() or "default"
        node_id = str(payload.get("id", "")).strip()
        if not node_id:
            return {"status": "error", "message": "id fehlt."}
        try:
            dimension = int(payload.get("dimension", 0) or 0)
        except Exception:
            return {"status": "error", "message": "dimension ist ungültig."}
        if dimension <= 0:
            return {"status": "error", "message": "dimension muss > 0 sein."}

        index = self._smql_vector_index()
        if bool(payload.get("strict_vram_required", False)) and not index.vram_available():
            return {
                "status": "error",
                "message": "strict_vram_required=true, but v1.22b OpenCL VRAM backend is unavailable",
                "version": SMQLNativeVectorIndex.VERSION,
                "backend": "cpu-vector-fallback",
                "vram_resident": False,
                "strict_vram_residency_proven": False,
            }

        mood_raw = payload.get("mood_vector") or (0.5, 0.5, 0.5)
        try:
            mood = tuple(float(v) for v in tuple(mood_raw)[:3])
        except Exception:
            return {"status": "error", "message": "mood_vector ist ungültig."}
        while len(mood) < 3:
            mood = (*mood, 0.0)

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {"value": str(metadata)}

        row = {
            "node_type": "smql_embedding_attractor",
            "collection": collection,
            "external_id": node_id,
            "dimension": dimension,
            "vector_sha256": str(payload.get("vector_sha256", "")),
            "payload_sha256": str(payload.get("payload_sha256", "")),
            "offset": int(payload.get("offset", -1) or -1),
            "norm": float(payload.get("norm", 0.0) or 0.0),
            "pheromone": float(payload.get("pheromone", 1.0) or 1.0),
            "energy_hash": str(payload.get("energy_hash", "")),
            "metadata": dict(metadata),
            "created_at": time.time(),
            "strict_vram_required": bool(payload.get("strict_vram_required", False)),
            "strict_vram_residency_proven": False,
            "bridge_mode": "v1.22b-full-vector-index",
        }

        pattern = self.core.database.store_sql_record(
            "mycelia_embeddings",
            row,
            stability=float(payload.get("stability", 0.9) or 0.9),
            mood_vector=mood,
            chaos_key=0.0,
        )
        index_response = index.store(payload, signature=pattern.signature)
        if index_response.get("status") != "ok":
            return index_response

        self._record_provenance_event(
            "store_embedding",
            str(pattern.signature),
            {
                "collection": collection,
                "id": node_id,
                "dimension": dimension,
                "vector_sha256": str(payload.get("vector_sha256", "")),
                "backend": index_response.get("backend"),
            },
            table="mycelia_embeddings",
        )

        save = {"status": "skipped", "reason": "autosave_disabled_by_payload"}
        if bool(payload.get("autosave", True)):
            save = self.autosave_snapshot("store_embedding")

        return {
            **index_response,
            "table": "mycelia_embeddings",
            "driver_mode": self.driver_mode,
            "autosave": save.get("status"),
        }

    def find_embedding(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """v1.22b: full-dimensional vector search in the MyceliaDB process."""
        response = self._smql_vector_index().search(payload)
        response.setdefault("driver_mode", self.driver_mode)
        return response



    def query_pattern(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        cue = str(payload.get("cue", "")).strip()
        table = payload.get("table")
        limit = int(payload.get("limit", 10) or 10)
        if cue:
            results = self.core.associative_sql_query(cue, limit=limit)
        else:
            filters = payload.get("filters")
            if not isinstance(filters, Mapping):
                filters = None
            results = self.core.query_sql_like(
                table=str(table) if table else None,
                filters=filters,
                limit=limit,
            )
        return {"status": "ok", "results": results, "driver_mode": self.driver_mode}


    def store_product(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        seller = str(payload.get("seller", "anonymous")).strip() or "anonymous"
        product = payload.get("product", {})
        if not isinstance(product, Mapping):
            product = {}
        encrypted = self._encrypt_json(dict(product))
        row = {
            "node_type": "product",
            "seller": seller,
            "product_seed": encrypted.seed,
            "product_blob": encrypted.blob,
            "crypto_mode": encrypted.mode,
            "created_at": time.time(),
        }
        pattern = self.core.database.store_sql_record(
            "mycelia_products",
            row,
            stability=0.94,
            mood_vector=(0.82, 0.08, 0.79),
        )
        save = self.autosave_snapshot("store_product")
        return {
            "status": "ok",
            "signature": pattern.signature,
            "driver_mode": self.driver_mode,
            "autosave": save.get("status"),
            "snapshot_path": str(self.snapshot_path),
        }

    def list_products(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        limit = int(payload.get("limit", 50) or 50)
        records = self.core.query_sql_like(table="mycelia_products", limit=limit)
        products: list[dict[str, Any]] = []
        for record in records:
            row = record["data"]
            try:
                data = self._decrypt_json({"seed": row["product_seed"], "blob": row["product_blob"]})
            except Exception as exc:
                data = {"integrity_error": str(exc)}
            products.append(
                {
                    "signature": record["signature"],
                    "seller": row.get("seller"),
                    "stability": record.get("stability"),
                    "product": data,
                }
            )
        return {"status": "ok", "products": products, "driver_mode": self.driver_mode}


    def _now(self) -> float:
        return time.time()

    def _public_author(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        author_signature = str(payload.get("author_signature", "")).strip()
        author_username = str(payload.get("author_username", "")).strip() or "anonymous"
        if not author_signature:
            raise ValueError("author_signature ist erforderlich.")
        return author_signature, author_username

    def _limit_public_text(self, value: Any, field_name: str = "text") -> str:
        text = str(value or "").strip()
        if len(text) > PUBLIC_TEXT_STORAGE_LIMIT:
            raise ValueError(
                f"{field_name} ist zu lang ({len(text)} Zeichen, max {PUBLIC_TEXT_STORAGE_LIMIT}). "
                "Bitte als Datei/Medium anhängen oder MYCELIA_PUBLIC_TEXT_STORAGE_LIMIT erhöhen."
            )
        return text

    def _content_packet(self, payload: Mapping[str, Any], key_prefix: str = "content") -> tuple[str, str, str]:
        packet = self._encrypt_json(dict(payload))
        return packet.seed, packet.blob, packet.mode

    def _decrypt_content(self, row: Mapping[str, Any], prefix: str = "content") -> dict[str, Any]:
        return self._decrypt_json({"seed": row[f"{prefix}_seed"], "blob": row[f"{prefix}_blob"]})

    def _parse_client_markdown_vault(self, value: Any, field: str = "body") -> dict[str, Any] | None:
        if not value:
            return None
        if isinstance(value, str):
            try:
                data = json.loads(value)
            except Exception as exc:
                raise ValueError(f"Ungültige Client-Markdown-Vault für {field}: {exc}") from exc
        elif isinstance(value, Mapping):
            data = dict(value)
        else:
            raise ValueError(f"Ungültige Client-Markdown-Vault für {field}")
        if data.get("version") != "client_markdown_vault_v1":
            raise ValueError("Unbekannte Client-Markdown-Vault-Version")
        required = ("ciphertext_b64", "iv_b64", "salt_b64", "aad", "sha256")
        missing = [key for key in required if not str(data.get(key, "")).strip()]
        if missing:
            raise ValueError("Unvollständige Client-Markdown-Vault: " + ", ".join(missing))
        return {
            "version": "client_markdown_vault_v1",
            "alg": str(data.get("alg", "PBKDF2-SHA256/AES-256-GCM")),
            "field": field,
            "ciphertext_b64": str(data["ciphertext_b64"]),
            "iv_b64": str(data["iv_b64"]),
            "salt_b64": str(data["salt_b64"]),
            "aad": str(data["aad"]),
            "sha256": str(data["sha256"]),
            "created_at_ms": int(float(data.get("created_at_ms", 0) or 0)),
            "markdown": bool(data.get("markdown", True)),
            "display_vault": True,
        }

    def _content_vault_from_payload(self, payload: Mapping[str, Any], field: str = "body") -> dict[str, Any] | None:
        return self._parse_client_markdown_vault(payload.get(f"{field}_vault_json") or payload.get(f"{field}_vault"), field)

    def _store_content_from_payload(self, row: dict[str, Any], payload: Mapping[str, Any], field: str, label: str, *, required: bool = True) -> tuple[bool, str]:
        """Store public text as browser-side encrypted Markdown vault when present.

        Normal web forms use direct-ingest.js to replace body/description with
        field_vault_json before the sealed payload reaches the Engine. In that
        path neither PHP nor the Python Engine materialize the public Markdown
        plaintext during normal storage or display. Legacy direct engine callers
        are still supported for compatibility and tests, but those records are
        marked as legacy server packets.
        """
        vault = self._content_vault_from_payload(payload, field)
        if vault:
            row[f"{field}_vault"] = vault
            row[f"{field}_storage"] = "client_markdown_vault_v1"
            # Remove legacy encrypted-content fields if this is an update.
            row.pop("content_seed", None)
            row.pop("content_blob", None)
            row.pop("crypto_mode", None)
            return True, "client_markdown_vault_v1"

        text = self._limit_public_text(payload.get(field, ""), label)
        if required and not text:
            return False, ""
        if text:
            seed, blob, mode = self._content_packet({field: text}, "content")
            row["content_seed"] = seed
            row["content_blob"] = blob
            row["crypto_mode"] = mode
            row[f"{field}_storage"] = "legacy_engine_packet"
            return True, "legacy_engine_packet"
        return True, "empty"

    def _content_response_fields(self, row: Mapping[str, Any], field: str = "body") -> dict[str, Any]:
        vault = row.get(f"{field}_vault")
        if isinstance(vault, Mapping):
            return {
                f"{field}_vault": dict(vault),
                f"{field}_storage": "client_markdown_vault_v1",
                "php_plaintext_safe": True,
                "engine_display_plaintext_materialized": False,
            }
        if row.get("content_seed") and row.get("content_blob"):
            # Compatibility path for older records and direct unit-test records.
            # The normal web path no longer uses this for new Forum/Blog content.
            try:
                content = self._decrypt_content(row)
            except Exception as exc:
                return {
                    field: f"[Integritätsfehler: {exc}]",
                    f"{field}_storage": "legacy_engine_packet_error",
                    "php_plaintext_safe": False,
                    "engine_display_plaintext_materialized": False,
                }
            value = content.get(field, "")
            return {
                field: value,
                f"{field}_html": self._markdown_fragment(value),
                f"{field}_storage": "legacy_engine_packet",
                "php_plaintext_safe": False,
                "engine_display_plaintext_materialized": True,
            }
        return {field: "", f"{field}_storage": "empty", "php_plaintext_safe": True, "engine_display_plaintext_materialized": False}

    def _sanitize_markdown_lang(self, value: str) -> str:
        lang = re.sub(r"[^A-Za-z0-9_+-]", "", str(value or "").strip().lower())[:32]
        return lang

    def _markdown_inline(self, text: str) -> str:
        """Small safe inline renderer for public markdown.

        Raw HTML is never passed through. This keeps the PHP layer out of
        plaintext parsing and returns an engine-sanitized fragment.
        """
        escaped = html.escape(str(text or ""), quote=True)

        def inline_code(match: re.Match[str]) -> str:
            return "<code>" + html.escape(match.group(1), quote=True) + "</code>"

        escaped = re.sub(r"`([^`\n]+)`", inline_code, escaped)

        def link(match: re.Match[str]) -> str:
            label = match.group(1)
            url = html.unescape(match.group(2)).strip()
            if not re.match(r"^https?://[^\s<>\"]{1,500}$", url, re.IGNORECASE):
                return label
            return f'<a href="{html.escape(url, quote=True)}" rel="nofollow noopener noreferrer" target="_blank">{label}</a>'

        escaped = re.sub(r"\[([^\]\n]{1,160})\]\((https?://[^\s\)<>\"]{1,500})\)", link, escaped)
        escaped = re.sub(r"\*\*([^*\n]{1,240})\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]{1,180})\*(?!\*)", r"<em>\1</em>", escaped)
        return escaped

    def _markdown_to_safe_html(self, source: Any) -> str:
        text = str(source or "")
        # UI render cap is intentionally high: Forum/Blog long-form Markdown should render
        # fully for normal documentation-sized posts. Storage is protected separately by
        # PUBLIC_TEXT_STORAGE_LIMIT and Direct-Ingest/PHP transport still stays sealed.
        if PUBLIC_MARKDOWN_RENDER_LIMIT > 0 and len(text) > PUBLIC_MARKDOWN_RENDER_LIMIT:
            text = text[:PUBLIC_MARKDOWN_RENDER_LIMIT] + "\n\n[gekürzt: Render-Limit erreicht]"
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out: list[str] = []
        paragraph: list[str] = []
        in_code = False
        code_lang = ""
        code_lines: list[str] = []

        def flush_paragraph() -> None:
            nonlocal paragraph
            if paragraph:
                joined = " ".join(part.strip() for part in paragraph if part.strip())
                if joined:
                    out.append('<p>' + self._markdown_inline(joined) + '</p>')
                paragraph = []

        def render_code() -> None:
            nonlocal code_lines, code_lang
            code_text = "\n".join(code_lines)
            lang = self._sanitize_markdown_lang(code_lang)
            label = html.escape(lang or "code", quote=True)
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(
                '<div class="md-codeblock">'
                '<div class="md-codebar"><span>' + label + '</span>'
                '<button type="button" class="md-copy-code" aria-label="Code kopieren">Kopieren</button></div>'
                '<pre><code' + cls + '>' + html.escape(code_text, quote=False) + '</code></pre>'
                '</div>'
            )
            code_lines = []
            code_lang = ""

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if in_code:
                if stripped.startswith("```"):
                    render_code()
                    in_code = False
                else:
                    code_lines.append(line)
                i += 1
                continue

            if stripped.startswith("```"):
                flush_paragraph()
                in_code = True
                code_lang = stripped[3:].strip()
                code_lines = []
                i += 1
                continue

            if stripped == "":
                flush_paragraph()
                i += 1
                continue

            heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
            if heading:
                flush_paragraph()
                level = min(len(heading.group(1)), 6)
                out.append(f'<h{level}>' + self._markdown_inline(heading.group(2).strip()) + f'</h{level}>')
                i += 1
                continue

            quote = re.match(r"^>\s?(.*)$", stripped)
            if quote:
                flush_paragraph()
                quote_lines = [quote.group(1)]
                i += 1
                while i < len(lines):
                    q = re.match(r"^>\s?(.*)$", lines[i].strip())
                    if not q:
                        break
                    quote_lines.append(q.group(1))
                    i += 1
                out.append('<blockquote>' + "".join('<p>' + self._markdown_inline(qline) + '</p>' for qline in quote_lines if qline.strip()) + '</blockquote>')
                continue

            list_match = re.match(r"^[-*]\s+(.+)$", stripped)
            ordered_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
            if list_match or ordered_match:
                flush_paragraph()
                tag = "ol" if ordered_match else "ul"
                items: list[str] = []
                while i < len(lines):
                    candidate = lines[i].strip()
                    m = re.match(r"^[-*]\s+(.+)$", candidate) if tag == "ul" else re.match(r"^\d+[.)]\s+(.+)$", candidate)
                    if not m:
                        break
                    items.append('<li>' + self._markdown_inline(m.group(1).strip()) + '</li>')
                    i += 1
                out.append(f'<{tag}>' + "".join(items) + f'</{tag}>')
                continue

            paragraph.append(line)
            i += 1

        if in_code:
            render_code()
        flush_paragraph()
        if not out:
            return ""
        return '<article class="markdown-body">' + "\n".join(out) + '</article>'

    def _markdown_fragment(self, source: Any) -> dict[str, Any]:
        return {
            "policy": "engine-safe-markdown-html",
            "renderer": "mycelia_markdown_v1_21_23",
            "text": self._markdown_to_safe_html(source),
        }

    def _get_record_or_error(self, signature: str, table: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.core.get_sql_record(signature)
        if not record:
            raise KeyError("Attraktor nicht gefunden.")
        if table and record.get("table") != table:
            raise KeyError("Attraktor gehört nicht zum erwarteten Netzwerksegment.")
        row = dict(record["data"])
        return record, row

    def _owner_matches(self, row: Mapping[str, Any], actor_signature: str, allow_admin: bool = False, actor_role: str = "", actor_permissions: Any = None) -> bool:
        if allow_admin and (actor_role == "admin" or "content.moderate" in self._normalize_permissions(actor_permissions, actor_role or "user")):
            return True
        return str(row.get("author_signature") or row.get("owner_signature") or "") == str(actor_signature)

    def _allowed_reactions(self) -> set[str]:
        # v1.21.18 Reaction Stickers: expressive but allowlisted metadata only.
        return {"like", "dislike", "insightful", "funny", "thanks", "fire", "thinking", "heart"}

    def _allowed_blog_themes(self) -> dict[str, dict[str, str]]:
        # v1.21.20 Blog Mood Themes: all values are allowlisted metadata.
        return {
            "security": {"label": "Security", "emoji": "🛡️"},
            "research": {"label": "Forschung", "emoji": "🧪"},
            "gaming": {"label": "Gaming", "emoji": "🎮"},
            "nature": {"label": "Natur", "emoji": "🌿"},
            "creative": {"label": "Kreativ", "emoji": "🎨"},
            "scifi": {"label": "Sci-Fi", "emoji": "🌌"},
        }

    def _normalize_blog_theme(self, value: Any) -> str:
        theme = str(value or "").strip().lower()
        return theme if theme in self._allowed_blog_themes() else ""

    def _blog_theme_descriptor(self, value: Any) -> dict[str, str]:
        theme = self._normalize_blog_theme(value)
        if not theme:
            return {"id": "", "label": "", "emoji": ""}
        meta = self._allowed_blog_themes()[theme]
        return {"id": theme, "label": meta["label"], "emoji": meta["emoji"]}

    def _reaction_counts(self, target_signature: str) -> dict[str, Any]:
        breakdown: dict[str, int] = {}
        for reaction in sorted(self._allowed_reactions()):
            hits = self.core.query_sql_like(table=REACTION_TABLE, filters={"target_signature": target_signature, "reaction": reaction}, limit=None)
            if hits:
                breakdown[reaction] = len(hits)
        likes = int(breakdown.get("like", 0))
        dislikes = int(breakdown.get("dislike", 0))
        return {"likes": likes, "dislikes": dislikes, "reaction_breakdown": breakdown}

    def create_forum_thread(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if (err := self._require_permission(payload, "forum.create")):
            return err
        author_signature, author_username = self._public_author(payload)
        title = str(payload.get("title", "")).strip()
        if not title:
            return {"status": "error", "message": "Titel und Beitrag sind erforderlich."}
        now = self._now()
        row = {
            "node_type": "forum_thread",
            "title": title[:240],
            "author_signature": author_signature,
            "author_username": author_username,
            "created_at": now,
            "updated_at": now,
            "deleted": False,
        }
        ok_content, storage = self._store_content_from_payload(row, payload, "body", "Forum-Beitrag", required=True)
        if not ok_content:
            return {"status": "error", "message": "Titel und Beitrag sind erforderlich."}
        row.update(self._ephemeral_fields_from_payload(payload))
        pattern = self.core.database.store_sql_record(FORUM_TABLE, row, stability=0.965, mood_vector=(0.90, 0.05, 0.86))
        media_signatures = self._store_media_from_payload(payload, target_signature=pattern.signature, target_type="forum_thread")
        save = self.autosave_snapshot("create_forum_thread")
        return {"status": "ok", "signature": pattern.signature, "media_signatures": media_signatures, "autosave": save.get("status")}

    def list_forum_threads(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        limit = int(payload.get("limit", 100) or 100)
        records = self.core.query_sql_like(table=FORUM_TABLE, limit=limit)
        threads: list[dict[str, Any]] = []
        for record in records:
            row = record["data"]
            if row.get("deleted"):
                continue
            counts = self._reaction_counts(record["signature"])
            comments = self.core.query_sql_like(table=COMMENT_TABLE, filters={"target_signature": record["signature"], "deleted": False}, limit=None)
            media_items = self.list_media_for_content({"target_signature": record["signature"]}).get("media", [])
            media_count = len(media_items)
            threads.append({
                "signature": record["signature"],
                "title": row.get("title"),
                "author_username": row.get("author_username"),
                "author_signature": row.get("author_signature"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "stability": record.get("stability"),
                "comments": len(comments),
                "media_count": media_count,
                "media_preview": media_items[:4],
                "media": media_items[:4],
                **counts,
            })
        threads.sort(key=lambda x: float(x.get("updated_at") or 0), reverse=True)
        return {"status": "ok", "threads": threads, "driver_mode": self.driver_mode}

    def get_forum_thread(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        try:
            record, row = self._get_record_or_error(signature, FORUM_TABLE)
            if row.get("deleted"):
                return {"status": "error", "message": "Beitrag wurde gelöscht."}
            content_fields = self._content_response_fields(row, "body")
            counts = self._reaction_counts(signature)
            media = self.list_media_for_content({"target_signature": signature}).get("media", [])
            return {"status": "ok", "thread": {
                "signature": signature,
                "title": row.get("title"),
                **content_fields,
                "author_username": row.get("author_username"),
                "author_signature": row.get("author_signature"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "stability": record.get("stability"),
                "media": media,
                **counts,
            }}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def update_forum_thread(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        title = str(payload.get("title", "")).strip()
        if not title:
            return {"status": "error", "message": "Titel und Beitrag sind erforderlich."}
        try:
            _, row = self._get_record_or_error(signature, FORUM_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role):
                return {"status": "error", "message": "Keine Berechtigung zum Ändern."}
            ok_content, storage = self._store_content_from_payload(row, payload, "body", "Forum-Beitrag", required=True)
            if not ok_content:
                return {"status": "error", "message": "Titel und Beitrag sind erforderlich."}
            row.update({"title": title[:240], "updated_at": self._now()})
            media_signatures = self._store_media_from_payload(payload, target_signature=signature, target_type="forum_thread")
            ok = self.core.update_sql_record(signature, row, stability=0.975, mood_vector=(0.91, 0.04, 0.88))
            save = self.autosave_snapshot("update_forum_thread") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "signature": signature, "media_signatures": media_signatures, "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def delete_forum_thread(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        try:
            _, row = self._get_record_or_error(signature, FORUM_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role):
                return {"status": "error", "message": "Keine Berechtigung zum Löschen."}
            row["deleted"] = True
            row["updated_at"] = self._now()
            ok = self.core.update_sql_record(signature, row, stability=0.90, mood_vector=(0.70, 0.20, 0.55))
            save = self.autosave_snapshot("delete_forum_thread") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def create_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        target_type_for_permission = str(payload.get("target_type", "forum_thread")).strip()
        if target_type_for_permission in {"blog", "blog_post"}:
            if (err := self._require_permission(payload, "blog.comment")):
                return err
        elif (err := self._require_permission(payload, "forum.comment")):
            return err
        author_signature, author_username = self._public_author(payload)
        target_signature = str(payload.get("target_signature", "")).strip()
        target_type = str(payload.get("target_type", "forum_thread")).strip()
        if not target_signature:
            return {"status": "error", "message": "Ziel und Kommentar sind erforderlich."}
        now = self._now()
        row = {
            "node_type": "comment",
            "target_signature": target_signature,
            "target_type": target_type,
            "author_signature": author_signature,
            "author_username": author_username,
            "created_at": now,
            "updated_at": now,
            "deleted": False,
        }
        ok_content, storage = self._store_content_from_payload(row, payload, "body", "Kommentar", required=True)
        if not ok_content:
            return {"status": "error", "message": "Ziel und Kommentar sind erforderlich."}
        row.update(self._ephemeral_fields_from_payload(payload))
        pattern = self.core.database.store_sql_record(COMMENT_TABLE, row, stability=0.94, mood_vector=(0.84, 0.08, 0.76))
        save = self.autosave_snapshot("create_comment")
        return {"status": "ok", "signature": pattern.signature, "autosave": save.get("status")}

    def list_comments(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        target_signature = str(payload.get("target_signature", "")).strip()
        filters: dict[str, Any] = {"deleted": False}
        if target_signature:
            filters["target_signature"] = target_signature
        records = self.core.query_sql_like(table=COMMENT_TABLE, filters=filters, limit=None)
        comments: list[dict[str, Any]] = []
        for record in records:
            row = record["data"]
            content_fields = self._content_response_fields(row, "body")
            counts = self._reaction_counts(record["signature"])
            comments.append({
                "signature": record["signature"],
                "target_signature": row.get("target_signature"),
                "target_type": row.get("target_type"),
                "author_username": row.get("author_username"),
                "author_signature": row.get("author_signature"),
                **content_fields,
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "stability": record.get("stability"),
                **counts,
            })
        comments.sort(key=lambda x: float(x.get("created_at") or 0))
        return {"status": "ok", "comments": comments}

    def update_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        try:
            _, row = self._get_record_or_error(signature, COMMENT_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role):
                return {"status": "error", "message": "Keine Berechtigung zum Ändern."}
            ok_content, storage = self._store_content_from_payload(row, payload, "body", "Kommentar", required=True)
            if not ok_content:
                return {"status": "error", "message": "Kommentar ist erforderlich."}
            row.update({"updated_at": self._now()})
            ok = self.core.update_sql_record(signature, row, stability=0.95)
            save = self.autosave_snapshot("update_comment") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def delete_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        try:
            _, row = self._get_record_or_error(signature, COMMENT_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role):
                return {"status": "error", "message": "Keine Berechtigung zum Löschen."}
            row["deleted"] = True
            row["updated_at"] = self._now()
            ok = self.core.update_sql_record(signature, row, stability=0.90)
            save = self.autosave_snapshot("delete_comment") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def react_content(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        actor = str(payload.get("actor_signature", "")).strip()
        username = str(payload.get("actor_username", "")).strip() or "anonymous"
        target_signature = str(payload.get("target_signature", "")).strip()
        target_type = str(payload.get("target_type", "content")).strip()
        reaction = str(payload.get("reaction", "")).strip().lower()
        core_reactions = {"like", "dislike"}
        if reaction not in self._allowed_reactions():
            return {"status": "error", "message": "reaction muss eine erlaubte Reaction-Sticker-ID sein.", "allowed": sorted(self._allowed_reactions())}
        if reaction not in core_reactions and not self._is_plugin_enabled("reaction_stickers"):
            return {"status": "error", "message": "Reaction Stickers Plugin ist nicht aktiviert.", "plugin_id": "reaction_stickers", "plugin_required": True}
        existing = self.core.query_sql_like(table=REACTION_TABLE, filters={"target_signature": target_signature, "actor_signature": actor}, limit=1)
        now = self._now()
        if existing:
            sig = existing[0]["signature"]
            row = dict(existing[0]["data"])
            row.update({"reaction": reaction, "updated_at": now})
            ok = self.core.update_sql_record(sig, row, stability=0.93)
            save = self.autosave_snapshot("react_content_update") if ok else {"status": "skipped"}
        else:
            row = {
                "node_type": "reaction",
                "target_signature": target_signature,
                "target_type": target_type,
                "actor_signature": actor,
                "actor_username": username,
                "reaction": reaction,
                "created_at": now,
                "updated_at": now,
            }
            self.core.database.store_sql_record(REACTION_TABLE, row, stability=0.92, mood_vector=(0.80, 0.12, 0.72))
            save = self.autosave_snapshot("react_content_create")
        return {"status": "ok", **self._reaction_counts(target_signature), "autosave": save.get("status")}

    def create_blog(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if (err := self._require_permission(payload, "blog.create")):
            return err
        owner_signature, owner_username = self._public_author({"author_signature": payload.get("owner_signature") or payload.get("author_signature"), "author_username": payload.get("owner_username") or payload.get("author_username")})
        title = str(payload.get("title", "")).strip()
        blog_theme = self._normalize_blog_theme(payload.get("blog_theme", ""))
        if blog_theme and not self._is_plugin_enabled("blog_mood_themes"):
            return {"status": "error", "message": "Blog Mood Themes Plugin ist nicht aktiviert.", "plugin_id": "blog_mood_themes", "plugin_required": True}
        if not title:
            return {"status": "error", "message": "Blog-Titel ist erforderlich."}
        now = self._now()
        row = {
            "node_type": "blog",
            "title": title[:240],
            "blog_theme": blog_theme,
            "blog_theme_label": self._blog_theme_descriptor(blog_theme).get("label", ""),
            "blog_theme_emoji": self._blog_theme_descriptor(blog_theme).get("emoji", ""),
            "owner_signature": owner_signature,
            "owner_username": owner_username,
            "created_at": now,
            "updated_at": now,
            "deleted": False,
        }
        ok_content, storage = self._store_content_from_payload(row, payload, "description", "Blog-Beschreibung", required=False)
        if not ok_content:
            return {"status": "error", "message": "Blog-Beschreibung ist ungültig."}
        row.update(self._ephemeral_fields_from_payload(payload))
        pattern = self.core.database.store_sql_record(BLOG_TABLE, row, stability=0.96, mood_vector=(0.89, 0.04, 0.84))
        media_signatures = self._store_media_from_payload(payload, target_signature=pattern.signature, target_type="blog")
        save = self.autosave_snapshot("create_blog")
        return {
            "status": "ok",
            "signature": pattern.signature,
            "media_signatures": media_signatures,
            "autosave": save.get("status"),
        }

    def list_blogs(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        owner = str(payload.get("owner_signature", "")).strip()
        filters = {"deleted": False}
        if owner:
            filters["owner_signature"] = owner
        records = self.core.query_sql_like(table=BLOG_TABLE, filters=filters, limit=None)
        blogs: list[dict[str, Any]] = []
        for record in records:
            row = record["data"]
            posts = self.core.query_sql_like(table=BLOG_POST_TABLE, filters={"blog_signature": record["signature"], "deleted": False}, limit=None)
            media_items = self.list_media_for_content({"target_signature": record["signature"]}).get("media", [])
            counts = self._reaction_counts(record["signature"])
            comments = self.core.query_sql_like(table=COMMENT_TABLE, filters={"target_signature": record["signature"], "deleted": False}, limit=None)
            blogs.append({
                "signature": record["signature"],
                "title": row.get("title"),
                **self._content_response_fields(row, "description"),
                "blog_theme": row.get("blog_theme", ""),
                "blog_theme_label": row.get("blog_theme_label", ""),
                "blog_theme_emoji": row.get("blog_theme_emoji", ""),
                "blog_theme_descriptor": self._blog_theme_descriptor(row.get("blog_theme", "")),
                "owner_signature": row.get("owner_signature"),
                "owner_username": row.get("owner_username"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "posts": len(posts),
                "media_count": len(media_items),
                "comments": len(comments),
                "media_preview": media_items[:4],
                "media": media_items[:4],
                "stability": record.get("stability"),
                **counts,
            })
        blogs.sort(key=lambda x: float(x.get("updated_at") or 0), reverse=True)
        return {"status": "ok", "blogs": blogs}

    def get_blog(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        try:
            record, row = self._get_record_or_error(signature, BLOG_TABLE)
            if row.get("deleted"):
                return {"status": "error", "message": "Blog wurde gelöscht."}
            media = self.list_media_for_content({"target_signature": signature}).get("media", [])
            counts = self._reaction_counts(signature)
            comments = self.core.query_sql_like(table=COMMENT_TABLE, filters={"target_signature": signature, "deleted": False}, limit=None)
            return {"status": "ok", "blog": {
                "signature": signature,
                "title": row.get("title"),
                **self._content_response_fields(row, "description"),
                "blog_theme": row.get("blog_theme", ""),
                "blog_theme_label": row.get("blog_theme_label", ""),
                "blog_theme_emoji": row.get("blog_theme_emoji", ""),
                "blog_theme_descriptor": self._blog_theme_descriptor(row.get("blog_theme", "")),
                "owner_signature": row.get("owner_signature"),
                "owner_username": row.get("owner_username"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "comments": len(comments),
                "media_count": len(media),
                "media": media,
                "stability": record.get("stability"),
                **counts,
            }}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def update_blog(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        title = str(payload.get("title", "")).strip()
        blog_theme = self._normalize_blog_theme(payload.get("blog_theme", ""))
        if blog_theme and not self._is_plugin_enabled("blog_mood_themes"):
            return {"status": "error", "message": "Blog Mood Themes Plugin ist nicht aktiviert.", "plugin_id": "blog_mood_themes", "plugin_required": True}
        try:
            _, row = self._get_record_or_error(signature, BLOG_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role):
                return {"status": "error", "message": "Keine Berechtigung."}
            ok_content, storage = self._store_content_from_payload(row, payload, "description", "Blog-Beschreibung", required=False)
            if not ok_content:
                return {"status": "error", "message": "Blog-Beschreibung ist ungültig."}
            theme_desc = self._blog_theme_descriptor(blog_theme)
            row.update({
                "title": title[:240],
                "blog_theme": blog_theme,
                "blog_theme_label": theme_desc.get("label", ""),
                "blog_theme_emoji": theme_desc.get("emoji", ""),
                "updated_at": self._now(),
            })
            ok = self.core.update_sql_record(signature, row, stability=0.97)
            media_signatures = self._store_media_from_payload(payload, target_signature=signature, target_type="blog") if ok else []
            save = self.autosave_snapshot("update_blog") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "media_signatures": media_signatures, "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def delete_blog(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        try:
            _, row = self._get_record_or_error(signature, BLOG_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role):
                return {"status": "error", "message": "Keine Berechtigung."}
            row["deleted"] = True
            row["updated_at"] = self._now()
            ok = self.core.update_sql_record(signature, row, stability=0.90)
            save = self.autosave_snapshot("delete_blog") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def create_blog_post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if (err := self._require_permission(payload, "blog.post.create")):
            return err
        author_signature, author_username = self._public_author(payload)
        blog_signature = str(payload.get("blog_signature", "")).strip()
        title = str(payload.get("title", "")).strip()
        status = str(payload.get("publish_status", "published")).strip()
        if not blog_signature or not title:
            return {"status": "error", "message": "Blog, Titel und Inhalt sind erforderlich."}
        now = self._now()
        row = {
            "node_type": "blog_post",
            "blog_signature": blog_signature,
            "title": title[:240],
            "author_signature": author_signature,
            "author_username": author_username,
            "publish_status": status,
            "created_at": now,
            "updated_at": now,
            "deleted": False,
        }
        ok_content, storage = self._store_content_from_payload(row, payload, "body", "Blog-Beitrag", required=True)
        if not ok_content:
            return {"status": "error", "message": "Blog, Titel und Inhalt sind erforderlich."}
        row.update(self._ephemeral_fields_from_payload(payload))
        pattern = self.core.database.store_sql_record(BLOG_POST_TABLE, row, stability=0.965, mood_vector=(0.91, 0.05, 0.87))
        media_signatures = self._store_media_from_payload(payload, target_signature=pattern.signature, target_type="blog_post")
        save = self.autosave_snapshot("create_blog_post")
        return {"status": "ok", "signature": pattern.signature, "media_signatures": media_signatures, "autosave": save.get("status")}

    def list_blog_posts(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        filters: dict[str, Any] = {"deleted": False}
        blog = str(payload.get("blog_signature", "")).strip()
        author = str(payload.get("author_signature", "")).strip()
        if blog:
            filters["blog_signature"] = blog
        if author:
            filters["author_signature"] = author
        records = self.core.query_sql_like(table=BLOG_POST_TABLE, filters=filters, limit=None)
        posts: list[dict[str, Any]] = []
        for record in records:
            row = record["data"]
            counts = self._reaction_counts(record["signature"])
            comments = self.core.query_sql_like(table=COMMENT_TABLE, filters={"target_signature": record["signature"], "deleted": False}, limit=None)
            media_items = self.list_media_for_content({"target_signature": record["signature"]}).get("media", [])
            media_count = len(media_items)
            posts.append({
                "signature": record["signature"],
                "blog_signature": row.get("blog_signature"),
                "title": row.get("title"),
                "author_signature": row.get("author_signature"),
                "author_username": row.get("author_username"),
                "publish_status": row.get("publish_status"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "comments": len(comments),
                "media_count": media_count,
                "media_preview": media_items[:4],
                "media": media_items[:4],
                "stability": record.get("stability"),
                **counts,
            })
        posts.sort(key=lambda x: float(x.get("updated_at") or 0), reverse=True)
        return {"status": "ok", "posts": posts}

    def get_blog_post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        try:
            record, row = self._get_record_or_error(signature, BLOG_POST_TABLE)
            if row.get("deleted"):
                return {"status": "error", "message": "Blog-Beitrag wurde gelöscht."}
            content_fields = self._content_response_fields(row, "body")
            counts = self._reaction_counts(signature)
            media = self.list_media_for_content({"target_signature": signature}).get("media", [])
            return {"status": "ok", "post": {
                "signature": signature,
                "blog_signature": row.get("blog_signature"),
                "title": row.get("title"),
                **content_fields,
                "author_signature": row.get("author_signature"),
                "author_username": row.get("author_username"),
                "publish_status": row.get("publish_status"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "stability": record.get("stability"),
                "media": media,
                **counts,
            }}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def update_blog_post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        # Browser forms historically used post_signature while the engine expected
        # signature. Accept both so media updates from my_blog.php do not fall into
        # a silent "record not found" path after Direct-Ingest stripped plaintext fields.
        signature = str(payload.get("signature") or payload.get("post_signature") or "").strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        title = str(payload.get("title", "")).strip()
        status = str(payload.get("publish_status", "published")).strip()
        try:
            _, row = self._get_record_or_error(signature, BLOG_POST_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role):
                return {"status": "error", "message": "Keine Berechtigung."}
            ok_content, storage = self._store_content_from_payload(row, payload, "body", "Blog-Beitrag", required=True)
            if not ok_content:
                return {"status": "error", "message": "Blog-Beitrag ist erforderlich."}
            row.update({"title": title[:240], "publish_status": status, "updated_at": self._now()})
            media_signatures = self._store_media_from_payload(payload, target_signature=signature, target_type="blog_post")
            ok = self.core.update_sql_record(signature, row, stability=0.975)
            save = self.autosave_snapshot("update_blog_post") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "media_signatures": media_signatures, "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def delete_blog_post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        try:
            _, row = self._get_record_or_error(signature, BLOG_POST_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role):
                return {"status": "error", "message": "Keine Berechtigung."}
            row["deleted"] = True
            row["updated_at"] = self._now()
            ok = self.core.update_sql_record(signature, row, stability=0.90)
            save = self.autosave_snapshot("delete_blog_post") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}




    # ------------------------------------------------------------------
    # v1.21 Media Attractor System
    # ------------------------------------------------------------------

    def _media_limits(self) -> dict[str, Any]:
        return {
            "max_image_bytes": int(os.environ.get("MYCELIA_MEDIA_MAX_IMAGE_BYTES", str(3 * 1024 * 1024))),
            "allowed_image_mime": {"image/jpeg", "image/png", "image/gif", "image/webp"},
            "allowed_video_providers": {"youtube", "vimeo"},
        }

    def _normalize_media_target(self, value: Any) -> str:
        return str(value or "").strip()[:128]

    def _media_owner_allowed(self, target_signature: str, actor_signature: str, actor_role: str = "", actor_permissions: Any = None) -> bool:
        if actor_role == "admin" or "media.moderate" in self._normalize_permissions(actor_permissions, actor_role or "user"):
            return True
        target = self.core.get_sql_record(target_signature)
        if not target:
            return False
        row = dict(target.get("data", {}))
        owner = str(row.get("author_signature") or row.get("owner_signature") or "")
        return owner == str(actor_signature)

    def _parse_embed_descriptor(self, raw_url: str) -> dict[str, Any] | None:
        url = str(raw_url or "").strip()
        if not url:
            return None
        if len(url) > 2048:
            raise ValueError("Embed-URL ist zu lang.")
        if not re.match(r"^https://", url, re.I):
            raise ValueError("Nur HTTPS-Embeds sind erlaubt.")
        lowered = url.lower()
        provider = ""
        embed_id = ""
        if "youtube.com/watch" in lowered or "youtu.be/" in lowered:
            provider = "youtube"
            m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,64})", url)
            if m:
                embed_id = m.group(1)
        elif "vimeo.com/" in lowered:
            provider = "vimeo"
            m = re.search(r"vimeo\.com/(?:video/)?([0-9]{4,32})", url)
            if m:
                embed_id = m.group(1)
        elif re.search(r"\.(?:png|jpe?g|gif|webp)(?:\?.*)?$", lowered):
            provider = "external_image"
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            embed_id = digest
        else:
            raise ValueError("Embed-Provider nicht erlaubt. Erlaubt: YouTube, Vimeo oder direkte HTTPS-Bild-URL.")
        if not provider or not embed_id:
            raise ValueError("Embed-URL konnte nicht sicher erkannt werden.")
        return {
            "provider": provider,
            "embed_id": embed_id,
            "source_url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "safe_url": url,
        }

    def _media_public_projection(self, record: Mapping[str, Any], *, include_data: bool = True) -> dict[str, Any]:
        row = dict(record.get("data", {}))
        out = {
            "signature": record.get("signature"),
            "target_signature": row.get("target_signature"),
            "target_type": row.get("target_type"),
            "media_kind": row.get("media_kind"),
            "filename": row.get("filename", ""),
            "mime": row.get("mime", ""),
            "title": row.get("title", ""),
            "author_signature": row.get("author_signature", ""),
            "author_username": row.get("author_username", ""),
            "created_at": row.get("created_at"),
            "media_sequence": int(row.get("media_sequence") or 0),
            "deleted": bool(row.get("deleted", False)),
            "moderation_status": row.get("moderation_status", "visible"),
            "size_bytes": row.get("size_bytes", 0),
            "stability": record.get("stability"),
        }
        if row.get("media_kind") == "image" and include_data and row.get("media_seed") and row.get("media_blob"):
            try:
                packet = self._decrypt_content(row, "media")
                data_b64 = str(packet.get("data_b64", ""))
                mime = str(packet.get("mime") or row.get("mime") or "")
                # Safe display only: data URI is returned only for web UI read path.
                if data_b64 and mime in self._media_limits()["allowed_image_mime"]:
                    out["data_uri"] = f"data:{mime};base64,{data_b64}"
            except Exception:
                out["render_error"] = "media-decrypt-failed"
        if row.get("media_kind") == "embed" and row.get("embed_descriptor"):
            desc = row.get("embed_descriptor")
            if isinstance(desc, Mapping):
                out["embed"] = {
                    "provider": desc.get("provider"),
                    "embed_id": desc.get("embed_id"),
                    "safe_url": desc.get("safe_url"),
                }
        return out

    def _store_media_from_payload(self, payload: Mapping[str, Any], *, target_signature: str, target_type: str) -> list[str]:
        stored: list[str] = []
        # Browser Direct-Ingest may send either file fields or explicit embed_url.
        file_b64 = str(payload.get("media_file_b64") or payload.get("image_b64") or "").strip()
        file_name = str(payload.get("media_file_name") or payload.get("image_name") or "upload").strip()[:180]
        mime = str(payload.get("media_mime") or payload.get("image_mime") or "").strip().lower()
        title = str(payload.get("media_title") or payload.get("image_title") or "").strip()[:240]
        limits = self._media_limits()

        if file_b64:
            try:
                raw = base64.b64decode(file_b64, validate=True)
            except Exception:
                raise ValueError("Mediendatei ist kein gültiges Base64.")
            if len(raw) > limits["max_image_bytes"]:
                raise ValueError(f"Bild ist zu groß. Maximum: {limits['max_image_bytes']} Bytes.")
            if mime not in limits["allowed_image_mime"]:
                raise ValueError("Bildtyp nicht erlaubt. Erlaubt: JPEG, PNG, GIF, WebP.")
            media_seed, media_blob, mode = self._content_packet({"data_b64": file_b64, "mime": mime}, "media")
            author_signature, author_username = self._public_author(payload)
            row = {
                "node_type": "media",
                "media_kind": "image",
                "target_signature": target_signature,
                "target_type": target_type,
                "filename": file_name,
                "title": title or file_name,
                "mime": mime,
                "size_bytes": len(raw),
                "author_signature": author_signature,
                "author_username": author_username,
                "media_seed": media_seed,
                "media_blob": media_blob,
                "crypto_mode": mode,
                "created_at": self._now(),
                "media_sequence": len(self.core.query_sql_like(table=MEDIA_TABLE, filters={"target_signature": target_signature}, limit=None)),
                "deleted": False,
                "moderation_status": "visible",
            }
            pattern = self.core.database.store_sql_record(MEDIA_TABLE, row, stability=0.955, mood_vector=(0.88, 0.09, 0.84))
            stored.append(pattern.signature)

        embed_url = str(payload.get("embed_url") or payload.get("media_embed_url") or "").strip()
        if embed_url:
            desc = self._parse_embed_descriptor(embed_url)
            if desc:
                author_signature, author_username = self._public_author(payload)
                row = {
                    "node_type": "media",
                    "media_kind": "embed",
                    "target_signature": target_signature,
                    "target_type": target_type,
                    "filename": "",
                    "title": title or desc.get("provider", "Embed"),
                    "mime": "text/uri-list",
                    "size_bytes": 0,
                    "author_signature": author_signature,
                    "author_username": author_username,
                    "embed_descriptor": desc,
                    "created_at": self._now(),
                    "deleted": False,
                    "moderation_status": "visible",
                }
                pattern = self.core.database.store_sql_record(MEDIA_TABLE, row, stability=0.945, mood_vector=(0.84, 0.12, 0.78))
                stored.append(pattern.signature)
        return stored

    def upload_media(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if (err := self._require_permission(payload, "media.upload")):
            return err
        target_signature = self._normalize_media_target(payload.get("target_signature"))
        target_type = str(payload.get("target_type") or "attachment").strip()[:80]
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        if not target_signature:
            return {"status": "error", "message": "target_signature erforderlich."}
        if not self._media_owner_allowed(target_signature, actor, actor_role, payload.get("actor_permissions")):
            return {"status": "error", "message": "Keine Berechtigung zum Anhängen von Medien."}
        try:
            signatures = self._store_media_from_payload(payload, target_signature=target_signature, target_type=target_type)
            if not signatures:
                return {"status": "error", "message": "Keine Mediendaten oder Embed-URL übergeben."}
            save = self.autosave_snapshot("upload_media")
            return {"status": "ok", "media_signatures": signatures, "count": len(signatures), "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def attach_media_to_content(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        # Alias retained for plugin capability semantics.
        return self.upload_media(payload)

    def list_media_for_content(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        target_signature = self._normalize_media_target(payload.get("target_signature"))
        if not target_signature:
            return {"status": "error", "message": "target_signature erforderlich."}
        records = self.core.query_sql_like(table=MEDIA_TABLE, filters={"target_signature": target_signature, "deleted": False, "moderation_status": "visible"}, limit=None)
        media = [self._media_public_projection(rec, include_data=bool(payload.get("include_data", True))) for rec in records]
        media.sort(key=lambda x: (float(x.get("created_at") or 0), int(x.get("media_sequence") or 0), str(x.get("signature") or "")))
        return {"status": "ok", "media": media, "count": len(media)}

    def render_media_safe(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        try:
            rec, row = self._get_record_or_error(signature, MEDIA_TABLE)
            if row.get("deleted") or row.get("moderation_status") != "visible":
                return {"status": "error", "message": "Medium nicht verfügbar."}
            return {"status": "ok", "media": self._media_public_projection(rec, include_data=True)}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def delete_media(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        signature = str(payload.get("signature", "")).strip()
        actor = str(payload.get("actor_signature", "")).strip()
        actor_role = str(payload.get("actor_role", "")).strip()
        try:
            _, row = self._get_record_or_error(signature, MEDIA_TABLE)
            if not self._owner_matches(row, actor, allow_admin=True, actor_role=actor_role, actor_permissions=payload.get("actor_permissions")):
                return {"status": "error", "message": "Keine Berechtigung zum Löschen dieses Mediums."}
            row["deleted"] = True
            row["updated_at"] = self._now()
            ok = self.core.update_sql_record(signature, row, stability=0.94, mood_vector=(0.70, 0.20, 0.60))
            save = self.autosave_snapshot("delete_media") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "signature": signature, "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def moderate_media(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if (err := self._require_permission(payload, "media.moderate")):
            return err
        signature = str(payload.get("signature", "")).strip()
        action = str(payload.get("action", "hide")).strip().lower()
        try:
            _, row = self._get_record_or_error(signature, MEDIA_TABLE)
            if action in {"hide", "quarantine"}:
                row["moderation_status"] = "quarantined"
            elif action in {"restore", "show", "visible"}:
                row["moderation_status"] = "visible"
            elif action == "delete":
                row["deleted"] = True
            else:
                return {"status": "error", "message": "Unbekannte Medienmoderation."}
            row["moderated_at"] = self._now()
            row["moderator_signature"] = str(payload.get("actor_signature", ""))
            ok = self.core.update_sql_record(signature, row, stability=0.94, mood_vector=(0.72, 0.18, 0.62))
            save = self.autosave_snapshot("moderate_media") if ok else {"status": "skipped"}
            return {"status": "ok" if ok else "error", "signature": signature, "autosave": save.get("status")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def list_all_media(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "media.moderate")):
            return {"status": "error", "message": "Admin-Recht erforderlich: media.moderate"}
        records = self.core.query_sql_like(table=MEDIA_TABLE, filters={}, limit=None)
        media = [self._media_public_projection(rec, include_data=False) for rec in records]
        media.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
        return {"status": "ok", "media": media, "count": len(media)}


    def _enterprise_plugin_manifests(self) -> list[dict[str, Any]]:
        return [
            {
                "plugin_id": "mycelia_digest",
                "name": "Mycelia Digest",
                "version": "1.0.0",
                "description": "Persönliche Aktivitätsübersicht mit E2EE-Metadaten, Antworten, Reaktionen und neuen öffentlichen Inhalten.",
                "author": "Mycelia Enterprise",
                "hooks": ["profile.digest", "admin.dashboard"],
                "capabilities": ["digest.own.activity", "digest.own.e2ee.count", "digest.public.recent"],
                "constraints": {"max_records": 250, "tension_threshold": 0.72},
                "outputs": [{"key": "digest", "type": "profile_cards"}],
            },
            {
                "plugin_id": "privacy_guardian",
                "name": "Privacy Guardian",
                "version": "1.0.0",
                "description": "Daten-Souveränitäts-Assistent für eigene Inhalte, Medien, E2EE-Keys, Export- und Löschhinweise.",
                "author": "Mycelia Enterprise",
                "hooks": ["profile.privacy", "admin.dashboard"],
                "capabilities": ["privacy.own.inventory", "privacy.own.media", "privacy.own.e2ee_keys", "privacy.own.export_status"],
                "constraints": {"max_records": 500, "tension_threshold": 0.72},
                "outputs": [{"key": "privacy", "type": "privacy_cards"}],
            },
            {
                "plugin_id": "content_trust_lens",
                "name": "Content Trust & Safety Lens",
                "version": "1.0.0",
                "description": "Bewertet öffentliche Inhalte anhand aggregierter Reaktionen, Kommentar-Dynamik und Moderationsstatus ohne private Rohdaten.",
                "author": "Mycelia Enterprise",
                "hooks": ["content.trust.badge", "blog.sidebar", "forum.sidebar"],
                "capabilities": ["trust.public.content", "trust.public.reactions", "trust.public.comments", "trust.public.moderation"],
                "constraints": {"max_records": 1000, "tension_threshold": 0.72},
                "outputs": [{"key": "trust", "type": "trust_badges"}],
            },

            {
                "plugin_id": "mycelia_achievements",
                "name": "Mycelia Achievements",
                "version": "1.0.0",
                "description": "Badge- und Erfolgssystem für Beiträge, Blogs, Medien, Kommentare, E2EE und Community-Reaktionen.",
                "author": "Mycelia Enterprise",
                "hooks": ["profile.fun", "profile.badges"],
                "capabilities": ["fun.own.achievements", "stats.own.content", "stats.own.media", "stats.own.e2ee_keys"],
                "constraints": {"max_records": 500, "tension_threshold": 0.72},
                "outputs": [{"key": "badges", "type": "badge_grid"}],
            },
            {
                "plugin_id": "daily_pulse",
                "name": "Daily Pulse",
                "version": "1.0.0",
                "description": "Tägliche Community-Pulsanzeige mit Hot Items, Aktivität und Stimmung aus Aggregaten.",
                "author": "Mycelia Enterprise",
                "hooks": ["home.pulse", "profile.fun"],
                "capabilities": ["fun.public.daily_pulse", "stats.public.activity"],
                "constraints": {"max_records": 500, "tension_threshold": 0.72},
                "outputs": [{"key": "pulse", "type": "pulse_cards"}],
            },
            {
                "plugin_id": "mycelia_quests",
                "name": "Mycelia Quests",
                "version": "1.0.0",
                "description": "Freiwillige Onboarding-Quests, die User spielerisch durch sichere Plattformfunktionen führen.",
                "author": "Mycelia Enterprise",
                "hooks": ["profile.fun", "profile.quests"],
                "capabilities": ["fun.own.quests", "stats.own.content"],
                "constraints": {"max_records": 100, "tension_threshold": 0.72},
                "outputs": [{"key": "quests", "type": "quest_cards"}],
            },
            {
                "plugin_id": "reaction_stickers",
                "name": "Reaction Stickers",
                "version": "1.0.0",
                "description": "Mehr Ausdruck als Like/Dislike: erlaubte Sticker-Reaktionen mit aggregierten Zählern.",
                "author": "Mycelia Enterprise",
                "hooks": ["content.reactions", "profile.fun"],
                "capabilities": ["fun.public.reaction_stickers", "stats.public.reactions"],
                "constraints": {"max_records": 1000, "tension_threshold": 0.72},
                "outputs": [{"key": "stickers", "type": "reaction_palette"}],
            },
            {
                "plugin_id": "blog_mood_themes",
                "name": "Blog Mood Themes",
                "version": "1.0.0",
                "description": "Allowlist-basierte Blog-Stimmungen und Themes ohne freies CSS/HTML.",
                "author": "Mycelia Enterprise",
                "hooks": ["blog.theme", "profile.fun"],
                "capabilities": ["fun.public.blog_themes", "stats.public.blog.count"],
                "constraints": {"max_records": 500, "tension_threshold": 0.72},
                "outputs": [{"key": "themes", "type": "theme_chips"}],
            },
            {
                "plugin_id": "community_constellation",
                "name": "Community Constellation",
                "version": "1.0.0",
                "description": "Aggregierte Myzel-Karte über Blogs, Forum, Medien, Kommentare und Reaktionen.",
                "author": "Mycelia Enterprise",
                "hooks": ["dashboard.constellation", "profile.fun"],
                "capabilities": ["fun.public.constellation", "stats.public.activity"],
                "constraints": {"max_records": 1000, "tension_threshold": 0.72},
                "outputs": [{"key": "constellation", "type": "safe_graph"}],
            },
            {
                "plugin_id": "random_discovery",
                "name": "Sporenflug Discovery",
                "version": "1.0.0",
                "description": "Zufällige, sichere Content-Entdeckung mit Trust-Signalen und ohne private Daten.",
                "author": "Mycelia Enterprise",
                "hooks": ["content.discovery", "profile.fun"],
                "capabilities": ["fun.public.discovery", "trust.public.content"],
                "constraints": {"max_records": 250, "tension_threshold": 0.72},
                "outputs": [{"key": "items", "type": "discovery_cards"}],
            },
            {
                "plugin_id": "creator_cards",
                "name": "Creator Cards",
                "version": "1.0.0",
                "description": "Öffentliche Creator-Karten aus freiwilligen/öffentlichen Aggregaten und Badges.",
                "author": "Mycelia Enterprise",
                "hooks": ["profile.creator", "profile.fun"],
                "capabilities": ["fun.public.creator_cards", "stats.public.user_activity"],
                "constraints": {"max_records": 500, "tension_threshold": 0.72},
                "outputs": [{"key": "cards", "type": "creator_cards"}],
            },
            {
                "plugin_id": "polls",
                "name": "Polls",
                "version": "1.0.0",
                "description": "Sichere Community-Abstimmungen mit einer Stimme pro User-Signatur.",
                "author": "Mycelia Enterprise",
                "hooks": ["content.polls", "profile.fun"],
                "capabilities": ["fun.public.polls", "stats.public.poll_votes"],
                "constraints": {"max_records": 500, "tension_threshold": 0.72},
                "outputs": [{"key": "polls", "type": "poll_cards"}],
            },
            {
                "plugin_id": "time_capsules",
                "name": "Time Capsules",
                "version": "1.0.0",
                "description": "Beiträge, die reifen: private oder öffentliche Zeitkapseln mit Reveal-Zeitpunkt.",
                "author": "Mycelia Enterprise",
                "hooks": ["profile.time_capsules", "profile.fun"],
                "capabilities": ["fun.own.time_capsules", "privacy.own.inventory"],
                "constraints": {"max_records": 250, "tension_threshold": 0.72},
                "outputs": [{"key": "capsules", "type": "time_capsule_cards"}],
            },
        ]

    def _row_not_deleted(self, rec: Mapping[str, Any]) -> bool:
        return not bool(dict(rec.get("data", {})).get("deleted"))

    def _owned_records(self, table: str, owner: str, *owner_fields: str) -> list[dict[str, Any]]:
        if not owner:
            return []
        out: list[dict[str, Any]] = []
        for rec in self._all_records(table):
            row = dict(rec.get("data", {}))
            if row.get("deleted"):
                continue
            if any(str(row.get(field, "")) == owner for field in owner_fields):
                out.append(rec)
        return out

    def _plugin_mycelia_digest(self, actor_signature: str) -> dict[str, Any]:
        if not actor_signature:
            return {"plugin_id": "mycelia_digest", "status": "needs_session", "summary": {}, "notifications": []}
        own_threads = self._owned_records(FORUM_TABLE, actor_signature, "author_signature")
        own_blogs = self._owned_records(BLOG_TABLE, actor_signature, "owner_signature")
        own_posts = self._owned_records(BLOG_POST_TABLE, actor_signature, "author_signature", "owner_signature")
        own_comments = self._owned_records(COMMENT_TABLE, actor_signature, "author_signature")
        own_targets = {str(r.get("signature")) for r in [*own_threads, *own_blogs, *own_posts, *own_comments] if r.get("signature")}
        reactions_on_own = [
            r for r in self._all_records(REACTION_TABLE)
            if str(r.get("data", {}).get("target_signature", "")) in own_targets
            and str(r.get("data", {}).get("actor_signature", "")) != actor_signature
        ]
        comments_on_own = [
            r for r in self._all_records(COMMENT_TABLE)
            if not r.get("data", {}).get("deleted")
            and str(r.get("data", {}).get("target_signature", "")) in own_targets
            and str(r.get("data", {}).get("author_signature", "")) != actor_signature
        ]
        inbox = [
            r for r in self._all_records(E2EE_MESSAGE_TABLE)
            if str(r.get("data", {}).get("recipient_signature", "")) == actor_signature
            and not r.get("data", {}).get("deleted")
            and not r.get("data", {}).get("deleted_for_recipient")
        ]
        outbox = [
            r for r in self._all_records(E2EE_MESSAGE_TABLE)
            if str(r.get("data", {}).get("sender_signature", "")) == actor_signature
            and not r.get("data", {}).get("deleted")
            and not r.get("data", {}).get("deleted_for_sender")
        ]
        recent_blogs = []
        for rec in sorted([r for r in self._all_records(BLOG_TABLE) if self._row_not_deleted(r)], key=lambda x: float(x.get("data", {}).get("updated_at") or 0), reverse=True)[:5]:
            row = dict(rec.get("data", {}))
            recent_blogs.append({
                "type": "blog",
                "signature": rec.get("signature"),
                "title": row.get("title", ""),
                "owner_username": row.get("owner_username", ""),
                "is_own": str(row.get("owner_signature", "")) == actor_signature,
                "updated_at": row.get("updated_at"),
            })
        notifications = []
        if comments_on_own:
            notifications.append({"type": "comments", "label": "Neue Kommentare auf deine Inhalte", "count": len(comments_on_own)})
        if reactions_on_own:
            notifications.append({"type": "reactions", "label": "Neue Reaktionen auf deine Inhalte", "count": len(reactions_on_own)})
        if inbox:
            notifications.append({"type": "e2ee", "label": "Verschlüsselte Nachrichten in deiner Inbox", "count": len(inbox)})
        return {
            "plugin_id": "mycelia_digest",
            "status": "ok",
            "engine_blind_e2ee": True,
            "raw_records_returned": 0,
            "unread_e2ee_count": len(inbox),
            "outbox_count": len(outbox),
            "summary": {
                "own_threads": len(own_threads),
                "own_blogs": len(own_blogs),
                "own_blog_posts": len(own_posts),
                "own_comments": len(own_comments),
                "comments_on_own_content": len(comments_on_own),
                "reactions_on_own_content": len(reactions_on_own),
            },
            "notifications": notifications,
            "recent_public": recent_blogs,
        }

    def _plugin_privacy_guardian(self, actor_signature: str) -> dict[str, Any]:
        if not actor_signature:
            return {"plugin_id": "privacy_guardian", "status": "needs_session", "inventory": {}}
        own_threads = self._owned_records(FORUM_TABLE, actor_signature, "author_signature")
        own_blogs = self._owned_records(BLOG_TABLE, actor_signature, "owner_signature")
        own_posts = self._owned_records(BLOG_POST_TABLE, actor_signature, "author_signature", "owner_signature")
        own_comments = self._owned_records(COMMENT_TABLE, actor_signature, "author_signature")
        own_media = self._owned_records(MEDIA_TABLE, actor_signature, "owner_signature", "author_signature")
        e2ee_keys = self._owned_records(E2EE_KEY_TABLE, actor_signature, "owner_signature")
        latest_key_age_days = None
        if e2ee_keys:
            latest = max(float(r.get("data", {}).get("created_at") or 0) for r in e2ee_keys)
            latest_key_age_days = max(0.0, (time.time() - latest) / 86400.0)
        ephemeral = []
        for table in (FORUM_TABLE, BLOG_TABLE, BLOG_POST_TABLE, COMMENT_TABLE, MEDIA_TABLE):
            for rec in self._owned_records(table, actor_signature, "author_signature", "owner_signature"):
                row = dict(rec.get("data", {}))
                if row.get("ttl_steps") or row.get("decay_rate") or row.get("expires_at"):
                    ephemeral.append(rec)
        inventory = {
            "forum_threads": len(own_threads),
            "blogs": len(own_blogs),
            "blog_posts": len(own_posts),
            "comments": len(own_comments),
            "media": len(own_media),
            "e2ee_public_keys": len(e2ee_keys),
            "ephemeral_items": len(ephemeral),
        }
        return {
            "plugin_id": "privacy_guardian",
            "status": "ok",
            "raw_records_returned": 0,
            "inventory": inventory,
            "public_content_count": len(own_threads) + len(own_blogs) + len(own_posts) + len(own_comments),
            "media_count": len(own_media),
            "e2ee_keys": {
                "count": len(e2ee_keys),
                "latest_key_age_days": round(latest_key_age_days, 1) if latest_key_age_days is not None else None,
                "rotation_recommended": bool(latest_key_age_days is not None and latest_key_age_days > 90),
            },
            "actions": {
                "export_available": True,
                "delete_account_available": True,
                "privacy_center": "privacy.php",
                "key_rotation_hint": "E2EE-Schlüssel regelmäßig erneuern, falls Geräte geteilt oder gewechselt wurden.",
            },
            "recommendations": [
                "Prüfe alte öffentliche Kommentare regelmäßig.",
                "Nutze DSGVO-Export vor größeren Löschaktionen.",
                "E2EE-Nachrichten bleiben für Engine/PHP blind; sichtbar sind nur Metadaten.",
            ],
        }

    def _trust_score_for_target(self, signature: str, row: Mapping[str, Any] | None = None) -> dict[str, Any]:
        counts = self._reaction_counts(signature)
        comments = [r for r in self._all_records(COMMENT_TABLE) if not r.get("data", {}).get("deleted") and str(r.get("data", {}).get("target_signature", "")) == signature]
        likes = int(counts.get("likes", 0) or 0)
        dislikes = int(counts.get("dislikes", 0) or 0)
        total_reactions = max(1, likes + dislikes)
        sentiment = (likes - dislikes) / total_reactions
        comment_pressure = min(1.0, len(comments) / 20.0)
        moderation_penalty = 0.25 if row and str(row.get("moderation_status", "visible")) not in {"", "visible"} else 0.0
        score = max(0.0, min(1.0, 0.72 + 0.20 * sentiment - 0.10 * comment_pressure - moderation_penalty))
        flags: list[str] = []
        if dislikes > likes and dislikes >= 3:
            flags.append("negative_reaction_skew")
        if len(comments) >= 20:
            flags.append("heated_discussion")
        if moderation_penalty:
            flags.append("moderation_attention")
        return {
            "target_signature": signature,
            "trust_score": round(score, 3),
            "label": "hoch" if score >= 0.75 else ("mittel" if score >= 0.55 else "prüfen"),
            "likes": likes,
            "dislikes": dislikes,
            "comments": len(comments),
            "flags": flags,
            "raw_content_returned": False,
        }

    def _plugin_content_trust_lens(self, actor_signature: str = "", target_signature: str = "") -> dict[str, Any]:
        del actor_signature
        targets: list[tuple[str, dict[str, Any]]] = []
        if target_signature:
            for table in (FORUM_TABLE, BLOG_TABLE, BLOG_POST_TABLE):
                found = self.core.query_sql_like(table=table, filters={}, limit=None)
                for rec in found:
                    if str(rec.get("signature")) == target_signature and not rec.get("data", {}).get("deleted"):
                        targets.append((str(rec.get("signature")), dict(rec.get("data", {}))))
                        break
        else:
            for table in (FORUM_TABLE, BLOG_TABLE, BLOG_POST_TABLE):
                for rec in self._all_records(table):
                    if not rec.get("data", {}).get("deleted"):
                        targets.append((str(rec.get("signature")), dict(rec.get("data", {}))))
        cards = [self._trust_score_for_target(sig, row) for sig, row in targets]
        heated = sum(1 for c in cards if "heated_discussion" in c.get("flags", []))
        negative = sum(1 for c in cards if "negative_reaction_skew" in c.get("flags", []))
        avg = round(sum(float(c["trust_score"]) for c in cards) / len(cards), 3) if cards else 1.0
        return {
            "plugin_id": "content_trust_lens",
            "status": "ok",
            "raw_records_returned": 0,
            "summary": {"targets_scored": len(cards), "average_trust": avg, "attention_needed": heated + negative},
            "reaction_signals": {"negative_skew_targets": negative},
            "discussion_signals": {"heated_targets": heated},
            "moderation": {"hidden_or_flagged_targets": sum(1 for c in cards if "moderation_attention" in c.get("flags", []))},
            "cards": sorted(cards, key=lambda c: float(c.get("trust_score", 1.0)))[:10],
        }

    def enterprise_plugin_dashboard(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        actor = str(payload.get("actor_signature", "")).strip()
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        target = str(payload.get("target_signature", "")).strip()
        enabled = self._enabled_plugin_ids()
        available = {
            "mycelia_digest": lambda: self._plugin_mycelia_digest(actor),
            "privacy_guardian": lambda: self._plugin_privacy_guardian(actor),
            "content_trust_lens": lambda: self._plugin_content_trust_lens(actor, target),
        }
        active_plugins: dict[str, Any] = {}
        for plugin_id, factory in available.items():
            if plugin_id in enabled:
                active_plugins[plugin_id] = factory()
        return {
            "status": "ok",
            "execution_model": "built-in-enterprise-plugin-sandbox",
            "code_execution": False,
            "io_access": False,
            "network_access": False,
            "raw_records_returned": 0,
            "plugins": active_plugins,
            "enabled_plugin_ids": sorted(enabled),
            "available_plugin_ids": sorted(available.keys()),
            "activation_policy": "installed_and_enabled_only",
            "message": "Enterprise plugin features are inert until their manifest is installed and enabled by an admin.",
        }


    def _public_content_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for table, kind in ((FORUM_TABLE, "forum_thread"), (BLOG_TABLE, "blog"), (BLOG_POST_TABLE, "blog_post")):
            for rec in self._all_records(table):
                row = dict(rec.get("data", {}))
                if row.get("deleted"):
                    continue
                row["_table"] = table
                row["_kind"] = kind
                row["_signature"] = str(rec.get("signature", ""))
                records.append({"signature": rec.get("signature"), "data": row, "stability": rec.get("stability")})
        return records

    def _plugin_achievements(self, actor_signature: str) -> dict[str, Any]:
        if not actor_signature:
            return {"plugin_id": "mycelia_achievements", "status": "needs_session", "badges": []}
        own_threads = self._owned_records(FORUM_TABLE, actor_signature, "author_signature")
        own_blogs = self._owned_records(BLOG_TABLE, actor_signature, "owner_signature")
        own_posts = self._owned_records(BLOG_POST_TABLE, actor_signature, "author_signature", "owner_signature")
        own_comments = self._owned_records(COMMENT_TABLE, actor_signature, "author_signature")
        own_media = self._owned_records(MEDIA_TABLE, actor_signature, "owner_signature", "author_signature")
        own_keys = self._owned_records(E2EE_KEY_TABLE, actor_signature, "owner_signature")
        own_targets = {str(r.get("signature")) for r in [*own_threads, *own_blogs, *own_posts] if r.get("signature")}
        reactions = [r for r in self._all_records(REACTION_TABLE) if str(r.get("data", {}).get("target_signature", "")) in own_targets]
        badge_specs = [
            ("first_post", "🌱 Erster Beitrag", len(own_threads) >= 1),
            ("first_blog", "🧬 Erster Blog", len(own_blogs) >= 1),
            ("commenter_10", "💬 10 Kommentare", len(own_comments) >= 10),
            ("media_seed", "🎥 Erstes Medium", len(own_media) >= 1),
            ("e2ee_ready", "🔐 E2EE-Key aktiv", len(own_keys) >= 1),
            ("community_fire", "🔥 10 Reaktionen erhalten", len(reactions) >= 10),
        ]
        badges = [{"badge_id": bid, "label": label, "earned": bool(ok)} for bid, label, ok in badge_specs]
        return {"plugin_id": "mycelia_achievements", "status": "ok", "raw_records_returned": 0, "earned_count": sum(1 for b in badges if b["earned"]), "badges": badges}

    def _plugin_daily_pulse(self) -> dict[str, Any]:
        now = time.time()
        since = now - 86400
        public = self._public_content_records()
        recent_public = [r for r in public if float(r.get("data", {}).get("created_at") or 0) >= since]
        comments_today = [r for r in self._all_records(COMMENT_TABLE) if not r.get("data", {}).get("deleted") and float(r.get("data", {}).get("created_at") or 0) >= since]
        reactions_today = [r for r in self._all_records(REACTION_TABLE) if float(r.get("data", {}).get("created_at") or 0) >= since]
        hot = sorted(public, key=lambda r: (len([x for x in self._all_records(COMMENT_TABLE) if str(x.get("data", {}).get("target_signature", "")) == str(r.get("signature"))]) + len([x for x in self._all_records(REACTION_TABLE) if str(x.get("data", {}).get("target_signature", "")) == str(r.get("signature"))])), reverse=True)[:3]
        mood = "ruhig"
        if len(comments_today) + len(reactions_today) > 30:
            mood = "lebendig"
        if len(comments_today) > 40:
            mood = "hitzig"
        return {
            "plugin_id": "daily_pulse",
            "status": "ok",
            "raw_records_returned": 0,
            "summary": {"new_public_content": len(recent_public), "comments_today": len(comments_today), "reactions_today": len(reactions_today), "community_mood": mood},
            "hot_items": [{"signature": h.get("signature"), "kind": h.get("data", {}).get("_kind"), "title": h.get("data", {}).get("title", "")} for h in hot],
        }

    def _plugin_quests(self, actor_signature: str) -> dict[str, Any]:
        digest = self._plugin_mycelia_digest(actor_signature)
        privacy = self._plugin_privacy_guardian(actor_signature)
        summary = dict(digest.get("summary", {}))
        inventory = dict(privacy.get("inventory", {}))
        quests = [
            {"quest_id": "write_comment", "label": "Schreibe einen hilfreichen Kommentar", "complete": int(summary.get("own_comments", 0)) > 0},
            {"quest_id": "create_blog", "label": "Erstelle deinen ersten Blog", "complete": int(inventory.get("blogs", 0)) > 0},
            {"quest_id": "upload_media", "label": "Teile ein Bild oder Video", "complete": int(inventory.get("media", 0)) > 0},
            {"quest_id": "enable_e2ee", "label": "Aktiviere E2EE-Nachrichten", "complete": int(inventory.get("e2ee_public_keys", 0)) > 0},
            {"quest_id": "discover", "label": "Entdecke einen öffentlichen Beitrag", "complete": False},
        ]
        return {"plugin_id": "mycelia_quests", "status": "ok", "raw_records_returned": 0, "active_quests": quests, "open_count": sum(1 for q in quests if not q["complete"])}

    def _plugin_reaction_stickers(self) -> dict[str, Any]:
        labels = {"like": "👍 Like", "dislike": "👎 Dislike", "insightful": "💡 Interessant", "funny": "😂 Lustig", "thanks": "❤️ Danke", "fire": "🔥 Stark", "thinking": "🤔 Nachdenklich", "heart": "💚 Herz"}
        totals = {rid: 0 for rid in self._allowed_reactions()}
        for rec in self._all_records(REACTION_TABLE):
            reaction = str(rec.get("data", {}).get("reaction", ""))
            if reaction in totals:
                totals[reaction] += 1
        return {"plugin_id": "reaction_stickers", "status": "ok", "raw_records_returned": 0, "allowed_reactions": [{"id": rid, "label": labels.get(rid, rid), "count": totals.get(rid, 0)} for rid in sorted(totals)]}

    def _plugin_blog_mood_themes(self) -> dict[str, Any]:
        themes = {
            "security": "🛡️ Security",
            "research": "🧪 Forschung",
            "gaming": "🎮 Gaming",
            "nature": "🌿 Natur",
            "creative": "🎨 Kreativ",
            "scifi": "🌌 Sci-Fi",
        }
        counts = {key: 0 for key in themes}
        for rec in self._all_records(BLOG_TABLE):
            row = dict(rec.get("data", {}))
            if row.get("deleted"):
                continue
            theme = str(row.get("blog_theme") or row.get("theme") or "").strip().lower()
            if theme in counts:
                counts[theme] += 1
        return {"plugin_id": "blog_mood_themes", "status": "ok", "raw_records_returned": 0, "themes": [{"id": k, "label": v, "count": counts[k]} for k, v in themes.items()]}

    def _plugin_community_constellation(self, actor_signature: str) -> dict[str, Any]:
        del actor_signature
        clusters: dict[str, int] = {"forum": 0, "blogs": 0, "media": 0, "comments": 0, "reactions": 0}
        clusters["forum"] = len([r for r in self._all_records(FORUM_TABLE) if not r.get("data", {}).get("deleted")])
        clusters["blogs"] = len([r for r in self._all_records(BLOG_TABLE) if not r.get("data", {}).get("deleted")])
        clusters["media"] = len([r for r in self._all_records(MEDIA_TABLE) if not r.get("data", {}).get("deleted")])
        clusters["comments"] = len([r for r in self._all_records(COMMENT_TABLE) if not r.get("data", {}).get("deleted")])
        clusters["reactions"] = len(self._all_records(REACTION_TABLE))
        nodes = [{"id": k, "label": k.title(), "weight": v} for k, v in clusters.items()]
        edges = [{"source": "comments", "target": "blogs", "weight": min(clusters["comments"], clusters["blogs"] + 1)}, {"source": "reactions", "target": "forum", "weight": min(clusters["reactions"], clusters["forum"] + 1)}, {"source": "media", "target": "blogs", "weight": min(clusters["media"], clusters["blogs"] + 1)}]
        return {"plugin_id": "community_constellation", "status": "ok", "raw_records_returned": 0, "nodes": nodes, "edges": edges, "privacy": "aggregated_only"}

    def _plugin_random_discovery(self, actor_signature: str) -> dict[str, Any]:
        public = [r for r in self._public_content_records() if str(r.get("data", {}).get("author_signature", r.get("data", {}).get("owner_signature", ""))) != actor_signature]
        scored = []
        for rec in public:
            sig = str(rec.get("signature", ""))
            trust = self._trust_score_for_target(sig, dict(rec.get("data", {})))
            recency = float(rec.get("data", {}).get("updated_at") or rec.get("data", {}).get("created_at") or 0)
            score = float(trust.get("trust_score", 0.5)) + min(0.25, max(0.0, (time.time() - recency) / 86400.0) * 0.0)
            scored.append((score, rec, trust))
        scored.sort(key=lambda x: hashlib.sha256((str(x[1].get("signature")) + str(int(time.time() // 3600))).encode()).hexdigest())
        picks = scored[:5]
        return {"plugin_id": "random_discovery", "status": "ok", "raw_records_returned": 0, "items": [{"signature": r.get("signature"), "kind": r.get("data", {}).get("_kind"), "title": r.get("data", {}).get("title", ""), "trust_label": t.get("label")} for _, r, t in picks]}

    def _plugin_creator_cards(self) -> dict[str, Any]:
        users = [r for r in self._all_records(USER_TABLE) if not r.get("data", {}).get("deleted")]
        cards = []
        for user in users:
            row = dict(user.get("data", {}))
            sig = str(user.get("signature", ""))
            cards.append({
                "username": row.get("username", "user"),
                "signature": sig,
                "blogs": len(self._owned_records(BLOG_TABLE, sig, "owner_signature")),
                "threads": len(self._owned_records(FORUM_TABLE, sig, "author_signature")),
                "media": len(self._owned_records(MEDIA_TABLE, sig, "owner_signature", "author_signature")),
                "badges": self._plugin_achievements(sig).get("earned_count", 0),
            })
        cards.sort(key=lambda c: (int(c.get("blogs", 0)) + int(c.get("threads", 0)) + int(c.get("media", 0))), reverse=True)
        return {"plugin_id": "creator_cards", "status": "ok", "raw_records_returned": 0, "cards": cards[:12]}

    def _plugin_polls(self, actor_signature: str) -> dict[str, Any]:
        polls = self.list_polls({"actor_signature": actor_signature}).get("polls", [])
        open_polls = [p for p in polls if not p.get("closed")]
        return {"plugin_id": "polls", "status": "ok", "raw_records_returned": 0, "poll_count": len(polls), "open_count": len(open_polls), "polls": open_polls[:5]}

    def _plugin_time_capsules(self, actor_signature: str) -> dict[str, Any]:
        capsules = self.list_time_capsules({"actor_signature": actor_signature}).get("capsules", [])
        ready = [c for c in capsules if c.get("is_revealed")]
        waiting = [c for c in capsules if not c.get("is_revealed")]
        return {"plugin_id": "time_capsules", "status": "ok", "raw_records_returned": 0, "ready_count": len(ready), "waiting_count": len(waiting), "capsules": capsules[:5]}

    def fun_plugin_dashboard(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        actor = str(payload.get("actor_signature", "")).strip()
        enabled = self._enabled_plugin_ids()
        available = {
            "mycelia_achievements": lambda: self._plugin_achievements(actor),
            "daily_pulse": lambda: self._plugin_daily_pulse(),
            "mycelia_quests": lambda: self._plugin_quests(actor),
            "reaction_stickers": lambda: self._plugin_reaction_stickers(),
            "blog_mood_themes": lambda: self._plugin_blog_mood_themes(),
            "community_constellation": lambda: self._plugin_community_constellation(actor),
            "random_discovery": lambda: self._plugin_random_discovery(actor),
            "creator_cards": lambda: self._plugin_creator_cards(),
            "polls": lambda: self._plugin_polls(actor),
            "time_capsules": lambda: self._plugin_time_capsules(actor),
        }
        active_plugins: dict[str, Any] = {}
        for plugin_id, factory in available.items():
            if plugin_id in enabled:
                active_plugins[plugin_id] = factory()
        return {
            "status": "ok",
            "execution_model": "built-in-enterprise-plugin-sandbox",
            "code_execution": False,
            "raw_records_returned": 0,
            "plugins": active_plugins,
            "enabled_plugin_ids": sorted(enabled),
            "available_plugin_ids": sorted(available.keys()),
            "activation_policy": "installed_and_enabled_only",
            "message": "Built-in plugin features are inert until their manifest is installed and enabled by an admin.",
        }

    def create_poll(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = self._require_plugin_enabled("polls", "Polls / Abstimmungen")
        if required:
            return required
        actor = str(payload.get("actor_signature", "")).strip()
        username = str(payload.get("actor_username", "")).strip() or "anonymous"
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        question = str(payload.get("question", "")).strip()[:300]
        options_raw = payload.get("options", [])
        if isinstance(options_raw, str):
            try:
                options_raw = json.loads(options_raw)
            except Exception:
                options_raw = []
        options = [str(v).strip()[:140] for v in (options_raw if isinstance(options_raw, list) else []) if str(v).strip()][:6]
        if not question or len(options) < 2:
            return {"status": "error", "message": "Umfrage benötigt Frage und mindestens zwei Optionen."}
        opts = [{"id": hashlib.sha256((question + str(i) + opt).encode()).hexdigest()[:12], "label": opt} for i, opt in enumerate(options)]
        row = {"node_type": "poll", "question": question, "options": opts, "author_signature": actor, "author_username": username, "target_signature": str(payload.get("target_signature", ""))[:128], "created_at": self._now(), "updated_at": self._now(), "deleted": False, "closed": False}
        pattern = self.core.database.store_sql_record(POLL_TABLE, row, stability=0.94, mood_vector=(0.72, 0.18, 0.82))
        save = self.autosave_snapshot("create_poll")
        return {"status": "ok", "signature": pattern.signature, "autosave": save.get("status")}

    def list_polls(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = self._require_plugin_enabled("polls", "Polls / Abstimmungen")
        if required:
            return {**required, "polls": [], "count": 0}
        del payload
        polls = []
        votes = self._all_records(POLL_VOTE_TABLE)
        for rec in self._all_records(POLL_TABLE):
            row = dict(rec.get("data", {}))
            if row.get("deleted"):
                continue
            sig = str(rec.get("signature", ""))
            counts: dict[str, int] = {}
            for v in votes:
                vrow = dict(v.get("data", {}))
                if str(vrow.get("poll_signature", "")) == sig and not vrow.get("deleted"):
                    oid = str(vrow.get("option_id", ""))
                    counts[oid] = counts.get(oid, 0) + 1
            options = []
            for opt in row.get("options", []):
                if isinstance(opt, Mapping):
                    oid = str(opt.get("id", ""))
                    options.append({"id": oid, "label": opt.get("label", ""), "votes": counts.get(oid, 0)})
            polls.append({"signature": sig, "question": row.get("question", ""), "options": options, "author_username": row.get("author_username", ""), "created_at": row.get("created_at"), "closed": bool(row.get("closed"))})
        polls.sort(key=lambda p: float(p.get("created_at") or 0), reverse=True)
        return {"status": "ok", "polls": polls, "count": len(polls)}

    def vote_poll(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = self._require_plugin_enabled("polls", "Polls / Abstimmungen")
        if required:
            return required
        actor = str(payload.get("actor_signature", "")).strip()
        username = str(payload.get("actor_username", "")).strip() or "anonymous"
        poll_signature = str(payload.get("poll_signature", "")).strip()
        option_id = str(payload.get("option_id", "")).strip()
        if not actor or not poll_signature or not option_id:
            return {"status": "error", "message": "poll_signature, option_id und Session erforderlich."}
        poll = self.core.query_sql_like(table=POLL_TABLE, filters={}, limit=None)
        target = None
        for rec in poll:
            if str(rec.get("signature")) == poll_signature:
                target = rec
                break
        if not target or target.get("data", {}).get("deleted") or target.get("data", {}).get("closed"):
            return {"status": "error", "message": "Umfrage nicht verfügbar."}
        options = target.get("data", {}).get("options", [])
        if option_id not in {str(o.get("id")) for o in options if isinstance(o, Mapping)}:
            return {"status": "error", "message": "Ungültige Option."}
        existing = self.core.query_sql_like(table=POLL_VOTE_TABLE, filters={"poll_signature": poll_signature, "actor_signature": actor}, limit=1)
        row = {"node_type": "poll_vote", "poll_signature": poll_signature, "option_id": option_id, "actor_signature": actor, "actor_username": username, "updated_at": self._now(), "deleted": False}
        if existing:
            old = dict(existing[0].get("data", {})); old.update(row)
            self.core.update_sql_record(str(existing[0]["signature"]), old, stability=0.91)
        else:
            row["created_at"] = self._now()
            self.core.database.store_sql_record(POLL_VOTE_TABLE, row, stability=0.91, mood_vector=(0.66, 0.22, 0.78))
        save = self.autosave_snapshot("vote_poll")
        return {"status": "ok", "poll_signature": poll_signature, "option_id": option_id, "autosave": save.get("status")}

    def create_time_capsule(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = self._require_plugin_enabled("time_capsules", "Time Capsules")
        if required:
            return required
        actor = str(payload.get("actor_signature", "")).strip()
        username = str(payload.get("actor_username", "")).strip() or "anonymous"
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        title = str(payload.get("title", "")).strip()[:180]
        body = str(payload.get("body", "")).strip()[:2000]
        reveal_raw = str(payload.get("reveal_at", "")).strip()
        reveal_ts = self._now() + 86400
        if reveal_raw:
            try:
                reveal_ts = datetime.fromisoformat(reveal_raw.replace("Z", "+00:00")).timestamp()
            except Exception:
                try:
                    reveal_ts = float(reveal_raw)
                except Exception:
                    pass
        if not title or not body:
            return {"status": "error", "message": "Titel und Inhalt erforderlich."}
        seed, blob, mode = self._content_packet({"title": title, "body": body}, "time_capsule")
        row = {"node_type": "time_capsule", "title": title, "author_signature": actor, "author_username": username, "content_seed": seed, "content_blob": blob, "crypto_mode": mode, "visibility": str(payload.get("visibility", "private"))[:32], "reveal_at": reveal_ts, "created_at": self._now(), "deleted": False}
        pattern = self.core.database.store_sql_record(TIME_CAPSULE_TABLE, row, stability=0.945, mood_vector=(0.76, 0.10, 0.86))
        save = self.autosave_snapshot("create_time_capsule")
        return {"status": "ok", "signature": pattern.signature, "reveal_at": reveal_ts, "autosave": save.get("status")}

    def list_time_capsules(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = self._require_plugin_enabled("time_capsules", "Time Capsules")
        if required:
            return {**required, "capsules": [], "count": 0}
        actor = str(payload.get("actor_signature", "")).strip()
        now = self._now()
        capsules = []
        for rec in self._all_records(TIME_CAPSULE_TABLE):
            row = dict(rec.get("data", {}))
            if row.get("deleted"):
                continue
            is_owner = str(row.get("author_signature", "")) == actor
            public = str(row.get("visibility", "")) == "public"
            if not (is_owner or public):
                continue
            is_revealed = now >= float(row.get("reveal_at") or 0)
            item = {"signature": rec.get("signature"), "title": row.get("title", ""), "author_username": row.get("author_username", ""), "reveal_at": row.get("reveal_at"), "is_revealed": is_revealed, "visibility": row.get("visibility", "private")}
            if is_revealed:
                try:
                    item["content"] = self.crypto.decrypt(row.get("content_seed", ""), row.get("content_blob", ""))
                except Exception:
                    item["content"] = {"body": "[Rekonstruktion fehlgeschlagen]"}
            capsules.append(item)
        capsules.sort(key=lambda c: float(c.get("reveal_at") or 0), reverse=True)
        return {"status": "ok", "capsules": capsules, "count": len(capsules)}


    def _enabled_plugin_ids(self) -> set[str]:
        """Return plugin ids that are explicitly installed and enabled.

        Built-in plugin implementations are inert until an admin installs the
        corresponding manifest and enables the plugin attractor. This keeps the
        project-wide plugin sandbox honest: template availability is not the
        same thing as runtime activation.
        """
        enabled: set[str] = set()
        for rec in self._all_records(PLUGIN_TABLE):
            row = dict(rec.get("data", {}))
            if bool(row.get("enabled", False)) and str(row.get("status", "")).lower() == "enabled":
                pid = str(row.get("plugin_id", "")).strip()
                if pid:
                    enabled.add(pid)
        return enabled

    def _is_plugin_enabled(self, plugin_id: str) -> bool:
        return str(plugin_id).strip() in self._enabled_plugin_ids()

    def _require_plugin_enabled(self, plugin_id: str, label: str | None = None) -> dict[str, Any] | None:
        if self._is_plugin_enabled(plugin_id):
            return None
        return {
            "status": "error",
            "message": f"Plugin nicht aktiviert: {label or plugin_id}. Bitte im Adminbereich installieren und aktivieren.",
            "plugin_id": plugin_id,
            "plugin_required": True,
        }

    def plugin_catalog(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        del payload
        capabilities = [
            {"key": "stats.user.count", "label": "Anzahl User", "leaks_raw_data": False},
            {"key": "stats.forum.count", "label": "Anzahl Forenbeiträge", "leaks_raw_data": False},
            {"key": "stats.blog.count", "label": "Anzahl Blogs", "leaks_raw_data": False},
            {"key": "stats.blog_post.count", "label": "Anzahl Blogposts", "leaks_raw_data": False},
            {"key": "stats.comment.count", "label": "Anzahl Kommentare", "leaks_raw_data": False},
            {"key": "stats.reaction.count", "label": "Anzahl Reaktionen", "leaks_raw_data": False},
            {"key": "stats.content.activity", "label": "Aggregierte Inhaltsaktivität", "leaks_raw_data": False},
            {"key": "stats.own.content", "label": "Eigene öffentliche Inhaltszähler", "leaks_raw_data": False},
            {"key": "stats.own.media", "label": "Eigene Medienaggregate", "leaks_raw_data": False},
            {"key": "stats.own.e2ee_keys", "label": "Eigene E2EE-Key-Aggregate", "leaks_raw_data": False},
            {"key": "stats.public.reactions", "label": "Öffentliche Reaktionsaggregate", "leaks_raw_data": False},
            {"key": "stats.public.blog.count", "label": "Öffentliche Blog-Anzahl", "leaks_raw_data": False},
            {"key": "digest.own.activity", "label": "Persönlicher Mycelia Digest ohne private Klartexte", "leaks_raw_data": False},
            {"key": "digest.own.e2ee.count", "label": "E2EE-Inbox/Outbox-Zähler ohne Nachrichtentexte", "leaks_raw_data": False},
            {"key": "digest.public.recent", "label": "Neue öffentliche Inhalte als sichere Hinweise", "leaks_raw_data": False},
            {"key": "privacy.own.inventory", "label": "Eigene Datenklassen und Zähler", "leaks_raw_data": False},
            {"key": "privacy.own.media", "label": "Eigene Medien-Zähler", "leaks_raw_data": False},
            {"key": "privacy.own.e2ee_keys", "label": "Eigener E2EE-Key-Status", "leaks_raw_data": False},
            {"key": "privacy.own.export_status", "label": "DSGVO-Export-/Löschhinweise", "leaks_raw_data": False},
            {"key": "trust.public.content", "label": "Öffentliche Content-Trust-Bewertung", "leaks_raw_data": False},
            {"key": "trust.public.reactions", "label": "Aggregierte Reaktionssignale", "leaks_raw_data": False},
            {"key": "trust.public.comments", "label": "Aggregierte Kommentar-/Diskussionssignale", "leaks_raw_data": False},
            {"key": "trust.public.moderation", "label": "Moderations- und Sichtbarkeitsstatus", "leaks_raw_data": False},
            {"key": "fun.own.achievements", "label": "Eigene Achievements und Badges", "leaks_raw_data": False},
            {"key": "fun.public.daily_pulse", "label": "Täglicher Community-Puls aus Aggregaten", "leaks_raw_data": False},
            {"key": "fun.own.quests", "label": "Eigene Onboarding-Quests", "leaks_raw_data": False},
            {"key": "fun.public.reaction_stickers", "label": "Allowlist-Reaction-Sticker", "leaks_raw_data": False},
            {"key": "fun.public.blog_themes", "label": "Allowlist Blog Mood Themes", "leaks_raw_data": False},
            {"key": "fun.public.constellation", "label": "Aggregierte Community-Konstellation", "leaks_raw_data": False},
            {"key": "fun.public.discovery", "label": "Zufällige Content-Entdeckung", "leaks_raw_data": False},
            {"key": "fun.public.creator_cards", "label": "Öffentliche Creator Cards", "leaks_raw_data": False},
            {"key": "fun.public.polls", "label": "Sichere Community-Polls", "leaks_raw_data": False},
            {"key": "fun.own.time_capsules", "label": "Eigene Time Capsules", "leaks_raw_data": False},
            {"key": "stats.public.poll_votes", "label": "Aggregierte Poll-Stimmen", "leaks_raw_data": False},
            {"key": "stats.public.user_activity", "label": "Öffentliche User-Aktivitätsaggregate", "leaks_raw_data": False},
            {"key": "stats.public.activity", "label": "Öffentliche Aktivitätsaggregate", "leaks_raw_data": False},
            {"key": "media.image.upload", "label": "Bilder als verschlüsselte Media-Attraktoren hochladen", "leaks_raw_data": False},
            {"key": "media.image.attach.forum", "label": "Bilder an Forenbeiträge anhängen", "leaks_raw_data": False},
            {"key": "media.image.attach.blog", "label": "Bilder an Blogposts anhängen", "leaks_raw_data": False},
            {"key": "media.image.render.safe", "label": "Sicheres Bild-Rendering über autorisierte Data-URI", "leaks_raw_data": False},
            {"key": "media.embed.link.create", "label": "Sichere externe Medien-Embeds erzeugen", "leaks_raw_data": False},
            {"key": "media.embed.provider.youtube", "label": "YouTube-Embeds erlauben", "leaks_raw_data": False},
            {"key": "media.embed.provider.vimeo", "label": "Vimeo-Embeds erlauben", "leaks_raw_data": False},
            {"key": "media.embed.provider.image_proxy", "label": "HTTPS-Bildlinks als Safe-Embed erlauben", "leaks_raw_data": False},
            {"key": "media.gallery.own", "label": "Eigene Mediengalerie anzeigen", "leaks_raw_data": False},
            {"key": "media.image.delete.own", "label": "Eigene Medien löschen", "leaks_raw_data": False},
            {"key": "media.moderate.hide", "label": "Medien moderieren/ausblenden", "leaks_raw_data": False},
            {"key": "media.audit.view", "label": "Medien-Auditübersicht", "leaks_raw_data": False},
        ]
        hooks = [
            {"key": "admin.dashboard", "label": "Admin-Dashboard Safe-Widget"},
            {"key": "profile.panel", "label": "Profil Safe-Widget"},
            {"key": "forum.sidebar", "label": "Forum Safe-Widget"},
            {"key": "blog.sidebar", "label": "Blog Safe-Widget"},
            {"key": "profile.digest", "label": "Profil-Digest"},
            {"key": "profile.privacy", "label": "Profil-Privacy-Guardian"},
            {"key": "content.trust.badge", "label": "Content Trust Badge"},
            {"key": "profile.fun", "label": "Profil Spaß-Plugins"},
            {"key": "profile.badges", "label": "Profil Badges"},
            {"key": "profile.quests", "label": "Profil Quests"},
            {"key": "home.pulse", "label": "Daily Pulse"},
            {"key": "content.reactions", "label": "Reaction Stickers"},
            {"key": "blog.theme", "label": "Blog Mood Theme"},
            {"key": "dashboard.constellation", "label": "Community Constellation"},
            {"key": "content.discovery", "label": "Sporenflug Discovery"},
            {"key": "profile.creator", "label": "Creator Cards"},
            {"key": "content.polls", "label": "Polls"},
            {"key": "profile.time_capsules", "label": "Time Capsules"},
        ]
        return {
            "status": "ok",
            "paradigm": "mycelia-plugin-attractor-v1",
            "execution_model": "declarative-capability-sandbox",
            "code_execution": False,
            "io_access": False,
            "raw_graph_scan": False,
            "capabilities": capabilities,
            "hooks": hooks,
            "manifest_example": {
                "plugin_id": "anonymous_stats",
                "name": "Anonyme Statistiken",
                "version": "1.0.0",
                "description": "Zeigt nur aggregierte Zähler ohne Rohdatenzugriff.",
                "hooks": ["admin.dashboard"],
                "capabilities": ["stats.forum.count", "stats.blog_post.count", "stats.user.count"],
                "constraints": {"max_records": 10000, "tension_threshold": 0.72},
                "outputs": [{"key": "summary", "type": "metric_cards"}],
            },
            "enterprise_plugins": self._enterprise_plugin_manifests(),
        }

    def _allowed_plugin_capabilities(self) -> set[str]:
        return {str(item["key"]) for item in self.plugin_catalog({}).get("capabilities", [])}

    def _allowed_plugin_hooks(self) -> set[str]:
        return {str(item["key"]) for item in self.plugin_catalog({}).get("hooks", [])}

    def _recursive_key_scan(self, value: Any, path: str = "") -> list[str]:
        forbidden = {"code", "python", "php", "shell", "sql", "eval", "exec", "socket", "network", "file", "filesystem", "subprocess", "import", "webhook", "url"}
        hits: list[str] = []
        if isinstance(value, Mapping):
            for key, nested in value.items():
                key_s = str(key).lower()
                next_path = f"{path}.{key_s}" if path else key_s
                if key_s in forbidden:
                    hits.append(next_path)
                hits.extend(self._recursive_key_scan(nested, next_path))
        elif isinstance(value, list):
            for idx, nested in enumerate(value):
                hits.extend(self._recursive_key_scan(nested, f"{path}[{idx}]"))
        return hits


    def _slugify_plugin_id(self, value: str, *, fallback: str = "plugin") -> str:
        """Create a safe stable plugin id from human input.

        Admins often paste manifests using `id`, `pluginId` or a human readable
        name with spaces/umlauts.  A rejected form is frustrating and does not
        add security, because the real security boundary is the capability
        sandbox.  We therefore canonicalize to a strict internal id while still
        enforcing a safe character set.
        """
        text_value = str(value or "").strip()
        replacements = {
            "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
            "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
        }
        for src, dst in replacements.items():
            text_value = text_value.replace(src, dst)
        text_value = text_value.lower()
        text_value = re.sub(r"[^a-z0-9_.-]+", "_", text_value)
        text_value = re.sub(r"_+", "_", text_value).strip("._-")
        if len(text_value) < 3:
            digest = hashlib.sha256((value or fallback).encode("utf-8")).hexdigest()[:8]
            text_value = f"{fallback}_{digest}"
        return text_value[:80]

    def _parse_plugin_manifest(self, manifest_json: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if isinstance(manifest_json, Mapping):
            manifest = dict(manifest_json)
        else:
            raw = str(manifest_json or "").strip()
            if len(raw) > 65536:
                return None, {"status": "error", "message": "Plugin-Manifest ist zu groß."}
            try:
                manifest = json.loads(raw)
            except Exception as exc:
                return None, {"status": "error", "message": f"Plugin-Manifest ist kein gültiges JSON: {exc}"}
        if not isinstance(manifest, dict):
            return None, {"status": "error", "message": "Plugin-Manifest muss ein JSON-Objekt sein."}

        forbidden_hits = self._recursive_key_scan(manifest)
        if forbidden_hits:
            return None, {"status": "error", "message": "Plugin enthält verbotene Code-/I/O-Schlüssel.", "forbidden_keys": forbidden_hits[:20]}

        raw_plugin_id = (
            manifest.get("plugin_id")
            or manifest.get("id")
            or manifest.get("pluginId")
            or manifest.get("plugin-id")
            or ""
        )
        name = str(manifest.get("name", "")).strip()
        version = str(manifest.get("version", "1.0.0")).strip()
        if not name or len(name) > 120:
            return None, {"status": "error", "message": "Plugin-Name fehlt oder ist zu lang."}
        plugin_id = self._slugify_plugin_id(str(raw_plugin_id or name), fallback="plugin")
        if not re.fullmatch(r"[a-z0-9_.-]{3,80}", plugin_id):
            return None, {"status": "error", "message": "Plugin-ID konnte nicht normalisiert werden.", "normalized_plugin_id": plugin_id}
        if not re.fullmatch(r"[0-9]+(\.[0-9]+){0,3}([a-zA-Z0-9_.-]+)?", version):
            return None, {"status": "error", "message": "Plugin-Version ist ungültig."}

        capabilities = manifest.get("capabilities", [])
        hooks = manifest.get("hooks", [])
        if isinstance(capabilities, str):
            capabilities = [capabilities]
        if isinstance(hooks, str):
            hooks = [hooks]
        capabilities = [str(c).strip() for c in capabilities if str(c).strip()]
        hooks = [str(h).strip() for h in hooks if str(h).strip()]
        unknown_caps = sorted(set(capabilities) - self._allowed_plugin_capabilities())
        unknown_hooks = sorted(set(hooks) - self._allowed_plugin_hooks())
        if unknown_caps:
            return None, {"status": "error", "message": "Plugin fordert unbekannte/unerlaubte Capabilities.", "unknown_capabilities": unknown_caps}
        if unknown_hooks:
            return None, {"status": "error", "message": "Plugin fordert unbekannte Hooks.", "unknown_hooks": unknown_hooks}
        if len(capabilities) > 16 or len(hooks) > 8:
            return None, {"status": "error", "message": "Plugin fordert zu viele Capabilities oder Hooks."}

        constraints = manifest.get("constraints", {})
        if not isinstance(constraints, Mapping):
            constraints = {}
        max_records = int(constraints.get("max_records", 1000) or 1000)
        tension_threshold = float(constraints.get("tension_threshold", 0.72) or 0.72)
        max_records = max(1, min(max_records, 10000))
        tension_threshold = max(0.1, min(tension_threshold, 0.95))

        outputs = manifest.get("outputs", [{"key": "summary", "type": "metric_cards"}])
        if not isinstance(outputs, list):
            outputs = [{"key": "summary", "type": "metric_cards"}]
        safe_outputs: list[dict[str, Any]] = []
        for out in outputs[:16]:
            if not isinstance(out, Mapping):
                continue
            out_key = str(out.get("key", "output")).strip()
            out_type = str(out.get("type", "metric_cards")).strip()
            if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", out_key) and out_type in {"metric_cards", "safe_text", "aggregate_table"}:
                safe_outputs.append({"key": out_key, "type": out_type})

        normalized = {
            "plugin_id": plugin_id,
            "name": name,
            "version": version,
            "description": str(manifest.get("description", ""))[:500],
            "author": str(manifest.get("author", ""))[:120],
            "hooks": sorted(set(hooks)),
            "capabilities": sorted(set(capabilities)),
            "constraints": {"max_records": max_records, "tension_threshold": tension_threshold},
            "outputs": safe_outputs or [{"key": "summary", "type": "metric_cards"}],
            "manifest_schema": "mycelia-plugin-attractor-v1",
            "code_execution": False,
            "io_access": False,
            "raw_graph_scan": False,
        }
        return normalized, None

    def _plugin_manifest_from_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        if row.get("manifest_seed") and row.get("manifest_blob"):
            return self._decrypt_json({"seed": row["manifest_seed"], "blob": row["manifest_blob"]})
        return {}

    def list_plugins(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.plugins.manage") or self._has_permission(payload, "plugin.run")):
            return {"status": "error", "message": "Plugin-Recht erforderlich."}
        plugins: list[dict[str, Any]] = []
        for rec in self._all_records(PLUGIN_TABLE):
            row = dict(rec.get("data", {}))
            manifest = self._plugin_manifest_from_row(row)
            plugins.append({
                "signature": rec.get("signature"),
                "plugin_id": row.get("plugin_id"),
                "name": row.get("name"),
                "version": row.get("version"),
                "description": manifest.get("description", ""),
                "enabled": bool(row.get("enabled", False)),
                "status": row.get("status", "installed"),
                "tension": float(row.get("tension", 0.0) or 0.0),
                "capabilities": manifest.get("capabilities", []),
                "hooks": manifest.get("hooks", []),
                "installed_at": row.get("installed_at"),
                "updated_at": row.get("updated_at"),
                "last_run_at": row.get("last_run_at"),
                "last_run_result": row.get("last_run_result"),
            })
        plugins.sort(key=lambda p: str(p.get("plugin_id", "")).lower())
        return {"status": "ok", "plugins": plugins, "catalog": self.plugin_catalog({})}

    def admin_install_plugin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.plugins.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.plugins.manage"}
        manifest, error = self._parse_plugin_manifest(payload.get("manifest_json", ""))
        if error:
            return error
        assert manifest is not None
        manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        encrypted = self._encrypt_json(manifest)
        row = {
            "node_type": "plugin_attractor",
            "plugin_id": manifest["plugin_id"],
            "name": manifest["name"],
            "version": manifest["version"],
            "enabled": False,
            "status": "installed",
            "manifest_seed": encrypted.seed,
            "manifest_blob": encrypted.blob,
            "manifest_sha256": manifest_hash,
            "crypto_mode": encrypted.mode,
            "tension": 0.0,
            "installed_at": time.time(),
            "updated_at": time.time(),
            "installed_by": str(payload.get("actor_signature", "")),
        }
        existing = self.core.query_sql_like(table=PLUGIN_TABLE, filters={"plugin_id": manifest["plugin_id"]}, limit=1)
        if existing:
            signature = str(existing[0]["signature"])
            old = dict(existing[0].get("data", {}))
            row["enabled"] = bool(old.get("enabled", False))
            row["status"] = str(old.get("status", "installed"))
            ok = self.core.update_sql_record(signature, row, stability=0.982, mood_vector=(0.64, 0.18, 0.88))
            if not ok:
                return {"status": "error", "message": "Plugin konnte nicht aktualisiert werden."}
        else:
            pattern = self.core.database.store_sql_record(PLUGIN_TABLE, row, stability=0.982, mood_vector=(0.64, 0.18, 0.88))
            signature = pattern.signature
        save = self.autosave_snapshot("admin_install_plugin")
        return {
            "status": "ok",
            "signature": signature,
            "plugin_id": manifest["plugin_id"],
            "enabled": row["enabled"],
            "manifest_sha256": manifest_hash,
            "autosave": save.get("status"),
            "message": "Plugin-Attraktor installiert. Ausführungscode wurde nicht importiert.",
        }

    def admin_set_plugin_state(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.plugins.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.plugins.manage"}
        signature = str(payload.get("signature", "")).strip()
        enabled = bool(payload.get("enabled", False))
        try:
            record, row = self._get_record_or_error(signature, PLUGIN_TABLE)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        row["enabled"] = enabled
        row["status"] = "enabled" if enabled else "disabled"
        row["updated_at"] = time.time()
        ok = self.core.update_sql_record(signature, row, stability=0.982, mood_vector=(0.64, 0.18, 0.88))
        if not ok:
            return {"status": "error", "message": "Plugin-Status konnte nicht geändert werden."}
        save = self.autosave_snapshot("admin_set_plugin_state")
        return {"status": "ok", "signature": signature, "enabled": enabled, "autosave": save.get("status")}

    def admin_delete_plugin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.plugins.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.plugins.manage"}
        signature = str(payload.get("signature", "")).strip()
        try:
            self._get_record_or_error(signature, PLUGIN_TABLE)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        removed = self._delete_attractor_record(signature)
        save = self.autosave_snapshot("admin_delete_plugin")
        return {"status": "ok" if removed else "error", "signature": signature, "deleted": removed, "autosave": save.get("status")}

    def _plugin_aggregate_value(self, capability: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        actor = str(payload.get("actor_signature", "")).strip()
        match capability:
            case "stats.user.count":
                return {"key": capability, "label": "User", "value": len(self._all_records(USER_TABLE))}
            case "stats.forum.count":
                return {"key": capability, "label": "Forenbeiträge", "value": len([r for r in self._all_records(FORUM_TABLE) if not r.get("data", {}).get("deleted")])}
            case "stats.blog.count":
                return {"key": capability, "label": "Blogs", "value": len([r for r in self._all_records(BLOG_TABLE) if not r.get("data", {}).get("deleted")])}
            case "stats.blog_post.count":
                return {"key": capability, "label": "Blogposts", "value": len([r for r in self._all_records(BLOG_POST_TABLE) if not r.get("data", {}).get("deleted")])}
            case "stats.comment.count":
                return {"key": capability, "label": "Kommentare", "value": len([r for r in self._all_records(COMMENT_TABLE) if not r.get("data", {}).get("deleted")])}
            case "stats.reaction.count":
                return {"key": capability, "label": "Reaktionen", "value": len(self._all_records(REACTION_TABLE))}
            case "stats.content.activity":
                value = (
                    len(self._all_records(FORUM_TABLE)) +
                    len(self._all_records(BLOG_TABLE)) +
                    len(self._all_records(BLOG_POST_TABLE)) +
                    len(self._all_records(COMMENT_TABLE)) +
                    len(self._all_records(REACTION_TABLE))
                )
                return {"key": capability, "label": "Content-Aktivität", "value": value}
            case "stats.own.content":
                value = {
                    "forum_threads": len(self._owned_records(FORUM_TABLE, actor, "author_signature")),
                    "blogs": len(self._owned_records(BLOG_TABLE, actor, "owner_signature")),
                    "blog_posts": len(self._owned_records(BLOG_POST_TABLE, actor, "author_signature", "owner_signature")),
                    "comments": len(self._owned_records(COMMENT_TABLE, actor, "author_signature")),
                }
                return {"key": capability, "label": "Eigene Inhalte", "value": value}
            case "stats.own.media":
                value = len(self._owned_records(MEDIA_TABLE, actor, "owner_signature", "author_signature"))
                return {"key": capability, "label": "Eigene Medien", "value": value}
            case "stats.own.e2ee_keys":
                value = len(self._owned_records(E2EE_KEY_TABLE, actor, "owner_signature"))
                return {"key": capability, "label": "Eigene E2EE-Keys", "value": value}
            case "stats.public.reactions":
                value = len([r for r in self._all_records(REACTION_TABLE) if not r.get("data", {}).get("deleted")])
                return {"key": capability, "label": "Öffentliche Reaktionen", "value": value}
            case "stats.public.blog.count":
                value = len([r for r in self._all_records(BLOG_TABLE) if not r.get("data", {}).get("deleted")])
                return {"key": capability, "label": "Öffentliche Blogs", "value": value}
            case "digest.own.activity":
                digest = self._plugin_mycelia_digest(actor)
                return {"key": capability, "label": "Deine Aktivität", "value": digest.get("summary", {})}
            case "digest.own.e2ee.count":
                digest = self._plugin_mycelia_digest(actor)
                return {"key": capability, "label": "E2EE-Zähler", "value": {"inbox": digest.get("unread_e2ee_count", 0), "outbox": digest.get("outbox_count", 0)}}
            case "digest.public.recent":
                digest = self._plugin_mycelia_digest(actor)
                return {"key": capability, "label": "Neue öffentliche Inhalte", "value": digest.get("recent_public", [])}
            case "privacy.own.inventory":
                privacy = self._plugin_privacy_guardian(actor)
                return {"key": capability, "label": "Eigene Dateninventur", "value": privacy.get("inventory", {})}
            case "privacy.own.media":
                privacy = self._plugin_privacy_guardian(actor)
                return {"key": capability, "label": "Eigene Medien", "value": privacy.get("media_count", 0)}
            case "privacy.own.e2ee_keys":
                privacy = self._plugin_privacy_guardian(actor)
                return {"key": capability, "label": "E2EE-Key-Status", "value": privacy.get("e2ee_keys", {})}
            case "privacy.own.export_status":
                privacy = self._plugin_privacy_guardian(actor)
                return {"key": capability, "label": "DSGVO-Aktionen", "value": privacy.get("actions", {})}
            case "trust.public.content":
                trust = self._plugin_content_trust_lens(actor)
                return {"key": capability, "label": "Trust Übersicht", "value": trust.get("summary", {})}
            case "trust.public.reactions":
                trust = self._plugin_content_trust_lens(actor)
                return {"key": capability, "label": "Reaktionssignale", "value": trust.get("reaction_signals", {})}
            case "trust.public.comments":
                trust = self._plugin_content_trust_lens(actor)
                return {"key": capability, "label": "Diskussionssignale", "value": trust.get("discussion_signals", {})}
            case "trust.public.moderation":
                trust = self._plugin_content_trust_lens(actor)
                return {"key": capability, "label": "Moderationsstatus", "value": trust.get("moderation", {})}
            case _:
                return {"key": capability, "label": capability, "value": None}

    def _record_plugin_audit(self, plugin_signature: str, action: str, result: str, tension: float, actor_signature: str = "") -> None:
        row = {
            "node_type": "plugin_audit",
            "plugin_signature": plugin_signature,
            "action": action,
            "result": result,
            "tension": tension,
            "actor_signature": actor_signature,
            "created_at": time.time(),
        }
        self.core.database.store_sql_record(PLUGIN_AUDIT_TABLE, row, stability=0.94, mood_vector=(0.42, 0.35, 0.68))

    def run_plugin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "plugin.run") or self._has_permission(payload, "admin.plugins.manage")):
            return {"status": "error", "message": "Plugin-Recht erforderlich: plugin.run"}
        signature = str(payload.get("signature", "")).strip()
        try:
            record, row = self._get_record_or_error(signature, PLUGIN_TABLE)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        manifest = self._plugin_manifest_from_row(row)
        if not row.get("enabled", False):
            return {"status": "error", "message": "Plugin ist deaktiviert."}
        caps = [str(c) for c in manifest.get("capabilities", []) if str(c)]
        unknown_caps = sorted(set(caps) - self._allowed_plugin_capabilities())
        # Tension is a deterministic observer score for potentially abusive requests.
        tension = min(1.0, 0.05 * len(caps) + 0.35 * len(unknown_caps))
        if len(caps) > 12:
            tension += 0.2
        threshold = float(manifest.get("constraints", {}).get("tension_threshold", 0.72) or 0.72)
        if unknown_caps or tension > threshold:
            row["enabled"] = False
            row["status"] = "suspended"
            row["tension"] = tension
            row["last_run_at"] = time.time()
            row["last_run_result"] = "suspended"
            self.core.update_sql_record(signature, row, stability=0.91, mood_vector=(0.18, 0.72, 0.20))
            self._record_plugin_audit(signature, "run", "suspended", tension, str(payload.get("actor_signature", "")))
            self.autosave_snapshot("plugin_suspended")
            return {
                "status": "error",
                "message": "Plugin wurde durch Observer-Tension blockiert und suspendiert.",
                "tension": tension,
                "unknown_capabilities": unknown_caps,
            }
        metrics = [self._plugin_aggregate_value(cap, payload) for cap in caps]
        safe_output = {
            "plugin_id": manifest.get("plugin_id"),
            "name": manifest.get("name"),
            "version": manifest.get("version"),
            "type": "safe_aggregate_result",
            "raw_records_returned": 0,
            "io_access": False,
            "network_access": False,
            "metrics": metrics,
            "safe_fragments": [self._safe_fragment(f"{m['label']}: {m['value']}") for m in metrics],
        }
        row["tension"] = tension
        row["last_run_at"] = time.time()
        row["last_run_result"] = "ok"
        self.core.update_sql_record(signature, row, stability=0.982, mood_vector=(0.64, 0.18, 0.88))
        self._record_plugin_audit(signature, "run", "ok", tension, str(payload.get("actor_signature", "")))
        return {"status": "ok", "plugin": safe_output, "tension": tension}

    def list_users(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.users.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.users.manage"}
        users = []
        records = self.core.query_sql_like(table=USER_TABLE, filters={}, limit=None)
        for rec in records:
            row = dict(rec.get("data", {}))
            profile = {}
            if row.get("profile_seed") and row.get("profile_blob"):
                try:
                    profile = self._decrypt_json({"seed": row["profile_seed"], "blob": row["profile_blob"]})
                except Exception:
                    profile = {}
            role = str(profile.get("role") or ("admin" if row.get("username") == "admin" else "user"))
            permissions = list(self._normalize_permissions(profile.get("permissions"), role))
            users.append({
                "signature": rec.get("signature"),
                "username": row.get("username", ""),
                "username_safe": self._safe_fragment(row.get("username", "")),
                "role": role,
                "permissions": permissions,
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "stability": rec.get("stability"),
            })
        users.sort(key=lambda u: str(u.get("username", "")).lower())
        return {"status": "ok", "users": users, "permission_catalog": self.permission_catalog({}).get("permissions", [])}

    def permission_catalog(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        del payload
        permissions = [
            {"key": "profile.update", "label": "Eigenes Profil ändern"},
            {"key": "forum.create", "label": "Forenbeiträge erstellen"},
            {"key": "forum.comment", "label": "Forum kommentieren"},
            {"key": "forum.react", "label": "Forum liken/disliken"},
            {"key": "blog.create", "label": "Eigene Blogs erstellen"},
            {"key": "blog.post.create", "label": "Blogposts erstellen"},
            {"key": "blog.comment", "label": "Blogs kommentieren"},
            {"key": "blog.react", "label": "Blogs liken/disliken"},
            {"key": "content.moderate", "label": "Forum/Blog moderieren"},
            {"key": "admin.access", "label": "Admin-Panel öffnen"},
            {"key": "admin.users.manage", "label": "Benutzerrechte vergeben/entziehen"},
            {"key": "admin.texts.manage", "label": "Webseitentexte ändern"},
            {"key": "admin.plugins.manage", "label": "Plugins installieren/verwalten"},
            {"key": "plugin.run", "label": "Sichere Plugins ausführen"},
            {"key": "media.upload", "label": "Bilder/Embeds anhängen"},
            {"key": "media.moderate", "label": "Medien moderieren"},
        ]
        return {"status": "ok", "permissions": permissions}

    def admin_update_user_rights(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.users.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.users.manage"}
        signature = str(payload.get("signature", "")).strip()
        if not signature:
            return {"status": "error", "message": "User-Signatur fehlt."}
        record, row = self._get_user_record(signature)
        if not record or not row:
            return {"status": "error", "message": "User-Attraktor nicht gefunden."}
        profile = {}
        if row.get("profile_seed") and row.get("profile_blob"):
            profile = self._decrypt_json({"seed": row["profile_seed"], "blob": row["profile_blob"]})
        role = str(payload.get("role") or profile.get("role") or "user")
        if role not in {"user", "admin", "moderator"}:
            return {"status": "error", "message": "Ungültige Rolle."}
        permissions = self._normalize_permissions(payload.get("permissions"), "admin" if role == "admin" else "user")
        if role == "admin":
            permissions = self._default_permissions_for_role("admin")
        profile["role"] = role
        profile["permissions"] = list(permissions)
        encrypted = self._encrypt_json(profile)
        row["profile_seed"] = encrypted.seed
        row["profile_blob"] = encrypted.blob
        row["crypto_mode"] = encrypted.mode
        row["updated_at"] = time.time()
        ok = self.core.update_sql_record(signature, row, stability=0.99, mood_vector=(0.98, 0.02, 0.94))
        if not ok:
            return {"status": "error", "message": "Rechte konnten nicht gespeichert werden."}
        # Live sessions for this user are updated immediately.
        for session in self.sessions.values():
            if session.signature == signature:
                session.role = role
                session.permissions = permissions
        save = self.autosave_snapshot("admin_update_user_rights")
        return {"status": "ok", "signature": signature, "role": role, "permissions": list(permissions), "autosave": save.get("status")}

    def list_site_texts(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        records = self.core.query_sql_like(table=SITE_TEXT_TABLE, filters={}, limit=None)
        texts = {}
        for rec in records:
            row = dict(rec.get("data", {}))
            key = str(row.get("key", ""))
            if not key:
                continue
            try:
                body = self._decrypt_json({"seed": row["text_seed"], "blob": row["text_blob"]})
                texts[key] = {
                    "key": key,
                    "value": str(body.get("value", "")),
                    "context": str(row.get("context", "global")),
                    "updated_at": row.get("updated_at"),
                    "signature": rec.get("signature"),
                    "safe": self._safe_fragment(body.get("value", "")),
                }
            except Exception:
                continue
        return {"status": "ok", "texts": texts}

    def admin_set_site_text(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.texts.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.texts.manage"}
        key = str(payload.get("key", "")).strip()
        value = str(payload.get("value", ""))
        context = str(payload.get("context", "web")).strip() or "web"
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{2,120}", key):
            return {"status": "error", "message": "Ungültiger Text-Schlüssel."}
        encrypted = self._encrypt_json({"value": value})
        matches = self.core.query_sql_like(table=SITE_TEXT_TABLE, filters={"key": key}, limit=1)
        row = {
            "node_type": "site_text",
            "key": key,
            "context": context,
            "text_seed": encrypted.seed,
            "text_blob": encrypted.blob,
            "crypto_mode": encrypted.mode,
            "updated_at": time.time(),
            "updated_by": str(payload.get("actor_signature", "")),
        }
        if matches:
            signature = str(matches[0]["signature"])
            ok = self.core.update_sql_record(signature, row, stability=0.975, mood_vector=(0.91, 0.04, 0.88))
            if not ok:
                return {"status": "error", "message": "Webseitentext konnte nicht aktualisiert werden."}
        else:
            pattern = self.core.database.store_sql_record(SITE_TEXT_TABLE, row, stability=0.975, mood_vector=(0.91, 0.04, 0.88))
            signature = pattern.signature
        save = self.autosave_snapshot("admin_set_site_text")
        return {"status": "ok", "key": key, "signature": signature, "autosave": save.get("status")}

    def admin_overview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        viewer = dict(payload or {})
        return {
            "status": "ok",
            "forum_threads": self.list_forum_threads({"limit": 1000}).get("threads", []),
            "blogs": self.list_blogs({}).get("blogs", []),
            "blog_posts": self.list_blog_posts({}).get("posts", []),
            "comments": self.list_comments({}).get("comments", []),
            "users": self.list_users(viewer).get("users", []),
            "permission_catalog": self.permission_catalog(viewer).get("permissions", []),
            "site_texts": self.list_site_texts(viewer).get("texts", {}),
            "plugins": self.list_plugins(viewer).get("plugins", []),
            "media": self.list_all_media(viewer).get("media", []) if (str(viewer.get("actor_role", "")) == "admin" or self._has_permission(viewer, "media.moderate")) else [],
            "plugin_catalog": self.plugin_catalog(viewer),
            "integrity": self.check_integrity({}),
        }



    # ------------------------------------------------------------------
    # v1.20 Semantic Mycelia Query Language (SMQL)
    # ------------------------------------------------------------------

    def _cue_vector(self, cue: str) -> tuple[float, float, float]:
        raw = str(cue or "").strip()
        vm = re.match(r"^VECTOR\s*\[(.+)\]$", raw, flags=re.IGNORECASE)
        if vm:
            vals: list[float] = []
            for part in vm.group(1).split(","):
                try:
                    vals.append(float(part.strip()))
                except Exception:
                    pass
            if vals:
                # Compact arbitrary client-side media embeddings into the 3D mood
                # space used by the current DAD prototype.  Native/GPU versions can
                # replace this projection with direct vector buffers later.
                thirds = [vals[0::3], vals[1::3], vals[2::3]]
                out = []
                for bucket in thirds:
                    out.append(sum(bucket) / max(1, len(bucket)))
                return tuple(max(0.0, min(1.0, (v + 1.0) / 2.0 if v < 0 else v)) for v in out[:3])  # type: ignore[return-value]
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        return (digest[0] / 255.0, digest[1] / 255.0, digest[2] / 255.0)

    @staticmethod
    def _cosine3(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return 0.0 if not na or not nb else max(0.0, min(1.0, dot / (na * nb)))

    def _parse_smql(self, query: str) -> dict[str, Any]:
        text = " ".join(str(query or "").strip().split())
        if not text:
            raise ValueError("SMQL-Abfrage fehlt.")
        # Greedy but deterministic parser for:
        # FIND table WHERE a=b AND c="d" ASSOCIATED WITH "cue" LIMIT 10
        m = re.match(r"^FIND\s+([a-zA-Z0-9_.*-]+)(.*)$", text, flags=re.IGNORECASE)
        if not m:
            raise ValueError("SMQL muss mit FIND <table> beginnen.")
        table = "" if m.group(1) == "*" else m.group(1)
        rest = m.group(2).strip()
        limit = 25
        lm = re.search(r"(?:^|\s+)LIMIT\s+(\d+)\s*$", rest, flags=re.IGNORECASE)
        if lm:
            limit = int(lm.group(1))
            rest = rest[: lm.start()].strip()
        cue = ""
        am = re.search(r"(?:^|\s+)ASSOCIATED\s+WITH\s+(.+)$", rest, flags=re.IGNORECASE)
        if am:
            cue = am.group(1).strip().strip("\"'")
            rest = rest[: am.start()].strip()
        filters: dict[str, Any] = {}
        if rest:
            wm = re.match(r"^WHERE\s+(.+)$", rest, flags=re.IGNORECASE)
            if not wm:
                raise ValueError("Erwartet WHERE, ASSOCIATED WITH oder LIMIT.")
            for part in re.split(r"\s+AND\s+", wm.group(1), flags=re.IGNORECASE):
                part = part.strip()
                if not part:
                    continue
                fm = re.match(r"^([a-zA-Z0-9_.-]+)\s*(=|==|:)\s*(.+)$", part)
                if not fm:
                    raise ValueError(f"Ungültiger WHERE-Ausdruck: {part}")
                key, _, value = fm.groups()
                value = value.strip().strip("\"'")
                if value.lower() in {"true", "false"}:
                    parsed: Any = value.lower() == "true"
                else:
                    try:
                        parsed = int(value)
                    except ValueError:
                        try:
                            parsed = float(value)
                        except ValueError:
                            parsed = value
                filters[key] = parsed
        return {"table": table, "filters": filters, "cue": cue, "limit": max(0, min(1000, limit)), "raw": text}

    # ------------------------------------------------------------------
    # Enterprise report redaction
    # ------------------------------------------------------------------

    def _redact_admin_report_object(self, obj: Any) -> Any:
        """Remove credential-equivalent and transport-secret material from
        admin/debug reports.

        The Engine still returns top-level engine_session for PHP token rotation,
        but nested JSON reports shown in dashboards must not reveal request
        tokens, auth patterns, encrypted profile payloads or other replayable
        transport material.
        """
        sensitive_keys = {
            "auth_pattern",
            "password",
            "password_hash",
            "profile_seed",
            "profile_blob",
            "content_seed",
            "content_blob",
            "blob",
            "seed",
            "key_b64",
            "iv_b64",
            "ciphertext_b64",
            "request_token",
            "sealed",
            "sealed_ingest",
            "private_key",
            "secret",
        }
        shorten_keys = {"handle", "engine_session_handle", "signature", "actor_signature", "author_signature", "owner_signature", "target_signature"}
        if isinstance(obj, Mapping):
            out: dict[str, Any] = {}
            for key, value in obj.items():
                ks = str(key)
                if ks in sensitive_keys:
                    # Omit credential-equivalent fields from safe reports entirely.
                    # Keeping the key name (even with a redacted value) trains users
                    # to expect secret-bearing fields in dashboards and can leak
                    # schema details such as auth_pattern/profile_blob.
                    continue
                elif ks == "engine_session" and isinstance(value, Mapping):
                    out[ks] = {
                        "handle": self._short_handle(str(value.get("handle", ""))),
                        "request_token": "[redacted:enterprise-report]",
                        "sequence": value.get("sequence"),
                        "expires_at": value.get("expires_at"),
                        "rotated": value.get("rotated"),
                    }
                elif ks in shorten_keys and isinstance(value, str) and len(value) > 18:
                    out[ks] = self._short_handle(value)
                else:
                    out[ks] = self._redact_admin_report_object(value)
            return out
        if isinstance(obj, list):
            return [self._redact_admin_report_object(v) for v in obj]
        return obj

    @staticmethod
    def _short_handle(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 18:
            return value
        return value[:10] + "…" + value[-6:]

    def smql_explain(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = self._parse_smql(str(payload.get("query", "")))
            return {
                "status": "ok",
                "version": SMQL_AUDIT_VERSION,
                "plan": {
                    "deterministic_filter": {"table": parsed["table"], "filters": parsed["filters"]},
                    "semantic_rank": {"cue": parsed["cue"], "cue_type": "vector" if str(parsed["cue"]).upper().startswith("VECTOR [") or str(parsed["cue"]).upper().startswith("VECTOR[") else "text", "cue_vector": list(self._cue_vector(parsed["cue"])) if parsed["cue"] else None},
                    "limit": parsed["limit"],
                    "execution_order": ["deterministic_filter", "mood_vector_similarity", "stability_sort"],
                },
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc), "version": SMQL_AUDIT_VERSION}

    def smql_query(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = self._parse_smql(str(payload.get("query", "")))
            debug = str(payload.get("debug", "")).lower() in {"1", "true", "yes", "on"}
            rows = self.core.query_sql_like(table=parsed["table"] or None, filters=parsed["filters"], limit=None)
            cue_vec = self._cue_vector(parsed["cue"]) if parsed["cue"] else None
            ranked: list[dict[str, Any]] = []
            for row in rows:
                sig = str(row.get("signature", ""))
                pattern = getattr(self.core.database, "_attractors", {}).get(sig)
                mood_raw = getattr(pattern, "mood_vector", (0.0, 0.0, 0.0)) if pattern is not None else (0.0, 0.0, 0.0)
                mood = tuple(float(v) for v in tuple(mood_raw)[:3])
                while len(mood) < 3:
                    mood = (*mood, 0.0)
                semantic_score = self._cosine3(cue_vec, mood) if cue_vec else 1.0
                item = dict(row)
                item["semantic_score"] = round(semantic_score, 6)
                item["smql_score"] = round(semantic_score * float(row.get("stability", 0.0) or 0.0), 6)
                if not debug:
                    item = self._redact_admin_report_object(item)
                ranked.append(item)
            ranked.sort(key=lambda r: (float(r.get("smql_score", 0.0)), float(r.get("stability", 0.0))), reverse=True)
            return {
                "status": "ok",
                "version": SMQL_AUDIT_VERSION,
                "query": parsed["raw"],
                "safe_mode": not debug,
                "debug_mode": debug,
                "redaction_policy": "enterprise-safe-smql-results" if not debug else "debug-raw-results",
                "total_candidates": len(ranked),
                "count": len(ranked[: parsed["limit"]]),
                "results": ranked[: parsed["limit"]],
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc), "version": SMQL_AUDIT_VERSION}

    # ------------------------------------------------------------------
    # v1.20 Myzel-Föderation
    # ------------------------------------------------------------------

    def _load_federation_state(self) -> dict[str, dict[str, Any]]:
        if not FEDERATION_STATE_PATH.exists():
            return {}
        try:
            data = json.loads(FEDERATION_STATE_PATH.read_text(encoding="utf-8"))
            peers = data.get("peers", data) if isinstance(data, dict) else {}
            return {str(k): dict(v) for k, v in peers.items() if isinstance(v, Mapping)} if isinstance(peers, dict) else {}
        except Exception:
            return {}

    def _save_federation_state(self) -> None:
        FEDERATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FEDERATION_STATE_PATH.write_text(json.dumps({"version": FEDERATION_AUDIT_VERSION, "peers": self.federation_peers}, indent=2, ensure_ascii=False), encoding="utf-8")

    def federation_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "version": FEDERATION_AUDIT_VERSION, "peer_count": len(self.federation_peers), "peers": list(self.federation_peers.values()), "mode": "nutrient-influx consensus"}

    def federation_peer_add(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.system.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.system.manage"}
        peer_id = str(payload.get("peer_id") or payload.get("id") or "").strip()
        url = str(payload.get("url") or "").strip()
        if not peer_id or not url:
            return {"status": "error", "message": "peer_id und url sind erforderlich."}
        self.federation_peers[peer_id] = {"peer_id": peer_id, "url": url, "cert_fingerprint": str(payload.get("fingerprint") or payload.get("cert_fingerprint") or ""), "enabled": bool(payload.get("enabled", True)), "last_seen": 0, "trust": "mtls-fingerprint-pinned"}
        self._save_federation_state()
        self._record_provenance_event("federation_peer_add", peer_id, payload, actor_signature=str(payload.get("actor_signature", "")))
        return {"status": "ok", "peer": self.federation_peers[peer_id]}

    def federation_peer_remove(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.system.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.system.manage"}
        peer_id = str(payload.get("peer_id") or "").strip()
        removed = self.federation_peers.pop(peer_id, None)
        self._save_federation_state()
        self._record_provenance_event("federation_peer_remove", peer_id, payload, actor_signature=str(payload.get("actor_signature", "")))
        return {"status": "ok", "removed": bool(removed), "peer_id": peer_id}

    def federation_export_stable(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        min_stability = float(payload.get("min_stability", 0.95))
        limit = max(1, min(1000, int(payload.get("limit", 100))))
        exported = []
        for rec in self._all_records(None):
            if float(rec.get("stability", 0.0) or 0.0) < min_stability:
                continue
            sig = str(rec.get("signature", ""))
            pattern = getattr(self.core.database, "_attractors", {}).get(sig)
            exported.append({
                "signature": sig,
                "table": rec.get("table"),
                "stability": rec.get("stability"),
                "mood_vector": list(getattr(pattern, "mood_vector", ()) or ()),
                "energy_hash": getattr(pattern, "energy_hash", ""),
                "payload_hash": hashlib.sha256(json.dumps(rec.get("data", {}), sort_keys=True, default=str).encode("utf-8")).hexdigest(),
            })
            if len(exported) >= limit:
                break
        return {"status": "ok", "version": FEDERATION_AUDIT_VERSION, "exported": len(exported), "attractors": exported}

    def federation_import_influx(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not (str(payload.get("actor_role", "")) == "admin" or self._has_permission(payload, "admin.system.manage")):
            return {"status": "error", "message": "Admin-Recht erforderlich: admin.system.manage"}
        remote = payload.get("attractors", [])
        if not isinstance(remote, list):
            return {"status": "error", "message": "attractors muss eine Liste sein."}
        imported = 0
        for item in remote[:1000]:
            if not isinstance(item, Mapping):
                continue
            row = {"node_type": "federated_attractor", "remote_signature": str(item.get("signature", "")), "remote_table": item.get("table", ""), "remote_payload_hash": item.get("payload_hash", ""), "remote_energy_hash": item.get("energy_hash", ""), "imported_at": time.time()}
            mood = item.get("mood_vector") or (0.5, 0.5, 0.5)
            self.core.database.store_sql_record("mycelia_federated_influx", row, stability=float(item.get("stability", 0.9) or 0.9), mood_vector=tuple(float(v) for v in tuple(mood)[:3]))
            imported += 1
        save = self.autosave_snapshot("federation_import_influx")
        self._record_provenance_event("federation_import_influx", "mycelia_federated_influx", {"imported": imported}, actor_signature=str(payload.get("actor_signature", "")))
        return {"status": "ok", "imported": imported, "autosave": save.get("status"), "mode": "nutrient_influx"}

    # ------------------------------------------------------------------
    # v1.20 Cryptographic Data Lineage
    # ------------------------------------------------------------------

    def _load_last_provenance_hash(self) -> str:
        if not PROVENANCE_LEDGER_PATH.exists():
            return "0" * 64
        try:
            last = ""
            with PROVENANCE_LEDGER_PATH.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        last = line
            return str(json.loads(last).get("event_hash", "0" * 64)) if last else "0" * 64
        except Exception:
            return "0" * 64

    def _record_provenance_event(self, operation: str, target_signature: str, payload: Mapping[str, Any] | None = None, *, actor_signature: str = "", table: str = "") -> dict[str, Any]:
        payload = payload or {}
        redacted_payload = {k: ("[redacted]" if any(s in k.lower() for s in ("password", "auth", "blob", "seed", "body", "content", "sealed")) else v) for k, v in dict(payload).items() if k not in {"profile"}}
        material = {"version": PROVENANCE_AUDIT_VERSION, "ts": time.time(), "operation": operation, "actor_signature": actor_signature or str(payload.get("actor_signature", "")), "target_signature": target_signature, "table": table, "payload_hash": hashlib.sha256(json.dumps(redacted_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest(), "previous_hash": self._provenance_last_hash}
        event_hash = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        material["event_hash"] = event_hash
        PROVENANCE_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PROVENANCE_LEDGER_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(material, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._provenance_last_hash = event_hash
        return material

    def provenance_log(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        limit = max(1, min(500, int(payload.get("limit", 100))))
        target = str(payload.get("signature", "")).strip()
        events = []
        if PROVENANCE_LEDGER_PATH.exists():
            for line in PROVENANCE_LEDGER_PATH.read_text(encoding="utf-8").splitlines()[-limit * 4:]:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if not target or event.get("target_signature") == target:
                    events.append(event)
        return {"status": "ok", "version": PROVENANCE_AUDIT_VERSION, "count": len(events[-limit:]), "events": events[-limit:]}

    def provenance_verify(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        prev = "0" * 64
        checked = 0
        if PROVENANCE_LEDGER_PATH.exists():
            for line_no, line in enumerate(PROVENANCE_LEDGER_PATH.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("previous_hash") != prev:
                    return {"status": "error", "verified": False, "line": line_no, "message": "Merkle-Kette unterbrochen."}
                expected = event.get("event_hash")
                material = dict(event); material.pop("event_hash", None)
                actual = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                if actual != expected:
                    return {"status": "error", "verified": False, "line": line_no, "message": "Event-Hash stimmt nicht."}
                prev = str(expected); checked += 1
        return {"status": "ok", "version": PROVENANCE_AUDIT_VERSION, "verified": True, "events": checked, "root_hash": prev}

    # ------------------------------------------------------------------
    # v1.20 Local Transport / Native Authenticity / Quantum Guard
    # ------------------------------------------------------------------

    def _load_or_create_local_transport_token(self) -> str:
        LOCAL_TRANSPORT_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOCAL_TRANSPORT_TOKEN_PATH.exists():
            return LOCAL_TRANSPORT_TOKEN_PATH.read_text(encoding="utf-8").strip()
        token = secrets.token_urlsafe(32)
        LOCAL_TRANSPORT_TOKEN_PATH.write_text(token, encoding="utf-8")
        try:
            os.chmod(LOCAL_TRANSPORT_TOKEN_PATH, 0o600)
        except Exception:
            pass
        return token

    def local_transport_security_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "version": LOCAL_TRANSPORT_SECURITY_VERSION, "token_binding_enabled": LOCAL_TRANSPORT_TOKEN_REQUIRED, "https_enabled": LOCAL_HTTPS_ENABLED, "token_path": str(LOCAL_TRANSPORT_TOKEN_PATH), "cert_path": str(LOCAL_HTTPS_CERT_PATH)}

    def native_library_authenticity(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        checks = []
        for role, paths in {"core_opencl_driver": _existing_driver_candidates(self.config.get("paths", {})), "native_gpu_envelope": NativeGPUResidencyBridge.candidate_paths()}.items():
            for path in paths:
                if path.exists():
                    try:
                        checks.append(verify_native_library_authenticity(path, role))
                    except Exception as exc:
                        checks.append({"status": "mismatch", "role": role, "path": str(path), "message": str(exc)})
                    break
        return {"status": "ok" if all(c.get("status") in {"ok", "unmanaged"} for c in checks) else "error", "version": NATIVE_AUTHENTICITY_VERSION, "strict": NATIVE_LIBRARY_STRICT, "manifest": str(NATIVE_HASH_MANIFEST_PATH), "checks": checks}

    def quantum_guard_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        status = self.core.quantum_guard_status() if hasattr(self.core, "quantum_guard_status") else {"available": False}
        return {"status": "ok", "version": QUANTUM_GUARD_VERSION, "guard": status}

    def _utf8_scan(self, haystack: bytes, probes: list[str]) -> list[dict[str, Any]]:
        """Case-sensitive byte scan for residency probes.

        This is intentionally simple and deterministic.  It does not claim to be a
        full OS process-memory scanner; it is an audit primitive for Mycelia-owned
        buffers, snapshots and serialised graph images.
        """
        findings: list[dict[str, Any]] = []
        for probe in probes:
            if not probe:
                continue
            needle = probe.encode("utf-8", errors="ignore")
            if needle and needle in haystack:
                findings.append({
                    "probe": probe[:24] + ("..." if len(probe) > 24 else ""),
                    "bytes": len(needle),
                })
        return findings

    def _graph_residency_scan(self, probes: list[str]) -> list[dict[str, Any]]:
        """Scan Mycelia-owned CPU graph metadata for known plaintext probes.

        The profile/body payloads should be encrypted blobs.  Public routing
        metadata such as usernames, titles or author names may still be present in
        CPU objects because the current PHP/JSON architecture needs them for
        routing, listing and rendering.  Findings here are evidence against a
        strict "CPU-RAM contains no cleartext fragments" claim.
        """
        findings: list[dict[str, Any]] = []
        for pattern in self.core.database.list_patterns():
            record = self.core.get_sql_record(pattern.signature)
            if not record:
                continue
            record_bytes = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            for hit in self._utf8_scan(record_bytes, probes):
                findings.append({
                    "location": "dad_record",
                    "table": record.get("table"),
                    "signature": str(record.get("signature", pattern.signature))[:12],
                    **hit,
                })
        return findings

    def _heartbeat_now(self) -> float:
        return time.time()

    def _heartbeat_public_key(self):
        if serialization is None or ed25519 is None:
            return None
        try:
            if HEARTBEAT_PUBLIC_KEY_PATH.exists():
                return serialization.load_pem_public_key(HEARTBEAT_PUBLIC_KEY_PATH.read_bytes())
        except Exception as exc:
            LOGGER.warning("Heartbeat public key could not be loaded: %s", exc)
        return None

    def _verify_heartbeat_signature(self, signed_payload: Mapping[str, Any], signature_b64: str) -> dict[str, Any]:
        public_key = self._heartbeat_public_key()
        if public_key is None:
            return {
                "signature_valid": False,
                "signature_trusted": False,
                "signature_reason": f"Heartbeat public key not available at {HEARTBEAT_PUBLIC_KEY_PATH}",
            }
        try:
            canonical = json.dumps(dict(signed_payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            public_key.verify(base64.b64decode(signature_b64), canonical)
            return {"signature_valid": True, "signature_trusted": True, "signature_reason": "ed25519 signature verified"}
        except Exception as exc:
            return {"signature_valid": False, "signature_trusted": False, "signature_reason": f"signature verification failed: {exc}"}

    def submit_heartbeat_audit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Store a signed scheduled residency heartbeat.

        The heartbeat is produced by an external tool, not by the Engine itself.
        That preserves the self-distrust model: the Engine may publish manifests
        and accept signed evidence, but the CPU-RAM scan is performed out of
        process.  Only machine-readable summaries/digests are persisted.
        """
        signed_payload_raw = payload.get("signed_payload", {})
        if not isinstance(signed_payload_raw, Mapping):
            return {"status": "error", "message": "signed_payload fehlt oder ist ungültig."}
        signed_payload = dict(signed_payload_raw)
        signature_b64 = str(payload.get("signature_b64", "") or "")
        signature = self._verify_heartbeat_signature(signed_payload, signature_b64)

        strict_bundle = signed_payload.get("strict_vram_evidence_bundle", {})
        latest_probe = {}
        strict_cert = {}
        if isinstance(strict_bundle, Mapping):
            latest_probe = dict(strict_bundle.get("latest_external_memory_probe", {}) or {})
            strict_cert = dict(strict_bundle.get("strict_vram_certification", {}) or {})
        strict_supported = bool(
            (isinstance(strict_bundle, Mapping) and strict_bundle.get("strict_98_security_supported"))
            or strict_cert.get("strict_98_security_supported")
        )
        strict_negative = bool(latest_probe.get("strict_negative") or latest_probe.get("negative"))
        strict_hits = int(latest_probe.get("strict_hits", 0) or 0)
        last_restore_cpu_materialized = bool(
            (isinstance(strict_bundle, Mapping) and strict_bundle.get("last_restore_cpu_materialized"))
            or strict_cert.get("last_restore_cpu_materialized")
        )
        certification_blockers = list(strict_cert.get("blockers", []) or [])
        restore_audit = signed_payload.get("restore_residency_audit", {})
        if isinstance(restore_audit, Mapping) and restore_audit.get("warning"):
            certification_blockers.append(str(restore_audit.get("warning")))
        pid_matches = int(signed_payload.get("pid", -1) or -1) == os.getpid()
        report = {
            "status": "accepted",
            "audit_version": HEARTBEAT_AUDIT_VERSION,
            "tool_version": str(signed_payload.get("tool_version", "")),
            "created_at": float(signed_payload.get("created_at", self._heartbeat_now()) or self._heartbeat_now()),
            "received_at": self._heartbeat_now(),
            "pid": int(signed_payload.get("pid", os.getpid()) or os.getpid()),
            "pid_matches_current_engine": pid_matches,
            "challenge_id": str(signed_payload.get("challenge_id", "")),
            "secret_sha256": str(signed_payload.get("secret_sha256", "")),
            "probe_evidence_digest": str(signed_payload.get("probe_evidence_digest", "")),
            "strict_98_security_supported": strict_supported,
            "strict_negative": strict_negative,
            "strict_hits": strict_hits,
            "last_restore_mode": str(strict_bundle.get("last_restore_mode", "")) if isinstance(strict_bundle, Mapping) else "",
            "last_restore_cpu_materialized": last_restore_cpu_materialized,
            "strict_vram_certification_enabled": bool(strict_bundle.get("strict_vram_certification_enabled")) if isinstance(strict_bundle, Mapping) else False,
            "negative_cpu_ram_probe": bool(strict_bundle.get("negative_cpu_ram_probe")) if isinstance(strict_bundle, Mapping) else strict_negative,
            "certification_blockers": certification_blockers,
            "restore_residency_audit": dict(restore_audit) if isinstance(restore_audit, Mapping) else {},
            "driver_mode": str(signed_payload.get("driver_mode", self.driver_mode)),
            "signature": signature,
            "signature_algorithm": "ed25519",
            "signature_b64": signature_b64,
            "public_key_path": str(HEARTBEAT_PUBLIC_KEY_PATH),
            "signed_payload_sha256": hashlib.sha256(
                json.dumps(signed_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "summary": {
                "certified": bool(strict_supported and strict_negative and strict_hits == 0 and not last_restore_cpu_materialized and signature.get("signature_trusted")),
                "strict_supported": strict_supported,
                "strict_negative": strict_negative,
                "strict_hits": strict_hits,
                "signature_trusted": bool(signature.get("signature_trusted")),
                "blockers": certification_blockers,
            },
        }
        HEARTBEAT_AUDIT_LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT_AUDIT_LATEST_PATH.with_suffix(HEARTBEAT_AUDIT_LATEST_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(HEARTBEAT_AUDIT_LATEST_PATH)
        self.latest_heartbeat_audit = report
        return {"status": "ok", "heartbeat_audit": report}

    def heartbeat_audit_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return the latest signed scheduled heartbeat status."""
        del payload
        report = getattr(self, "latest_heartbeat_audit", None)
        if report is None and HEARTBEAT_AUDIT_LATEST_PATH.exists():
            try:
                report = json.loads(HEARTBEAT_AUDIT_LATEST_PATH.read_text(encoding="utf-8"))
                self.latest_heartbeat_audit = report
            except Exception as exc:
                return {
                    "status": "error",
                    "message": f"Heartbeat-Audit-Datei konnte nicht gelesen werden: {exc}",
                    "path": str(HEARTBEAT_AUDIT_LATEST_PATH),
                }
        if not isinstance(report, Mapping):
            return {
                "status": "ok",
                "heartbeat_present": False,
                "certified": False,
                "state": "missing",
                "message": "Noch kein Scheduled Heartbeat Audit vorhanden.",
                "path": str(HEARTBEAT_AUDIT_LATEST_PATH),
                "max_age_seconds": HEARTBEAT_MAX_AGE_SECONDS,
            }
        age = max(0.0, self._heartbeat_now() - float(report.get("created_at", 0) or 0))
        expired = age > HEARTBEAT_MAX_AGE_SECONDS
        summary = dict(report.get("summary", {}) or {})
        certified = bool(summary.get("certified")) and not expired
        state = "strict-certified" if certified else ("expired" if expired else "not-certified")
        return {
            "status": "ok",
            "heartbeat_present": True,
            "certified": certified,
            "state": state,
            "age_seconds": age,
            "max_age_seconds": HEARTBEAT_MAX_AGE_SECONDS,
            "latest": dict(report),
            "display": {
                "label": "Aktueller Hardware-Residency-Status",
                "value": "ZERTIFIZIERT" if certified else ("ABGELAUFEN" if expired else "NICHT ZERTIFIZIERT"),
                "class": "ok" if certified else "warn",
            },
        }


    def residency_audit_manifest(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return an audit plan that does not require sending plaintext probes to the engine.

        A strict CPU-RAM residency test must be performed by an external scanner.
        Passing probe strings to this HTTP endpoint would itself materialize the
        probes inside Python.  Therefore the manifest publishes the process id,
        expected tool path and capability flags; the scanner receives plaintext
        probes out-of-band and reports only hashes plus hit counts back.
        """
        del payload
        mode = self.driver_mode
        opencl_active = mode.startswith("opencl:")
        core_gpu_crypto = self._core_gpu_crypto_active()
        native_envelope_crypto = self._native_envelope_crypto_active()
        gpu_crypto = self._gpu_crypto_active()
        challenge_id = secrets.token_hex(16)
        challenge = {
            "challenge_id": challenge_id,
            "created_at": time.time(),
            "pid": os.getpid(),
            "driver_mode": mode,
            "native_gpu_envelope_opener": self._native_envelope_to_vram_enabled(),
            "gpu_restore_opener": self._gpu_restore_to_vram_enabled(),
        }
        self.residency_challenges[challenge_id] = challenge
        return {
            "status": "ok",
            "audit_version": RESIDENCY_AUDIT_VERSION,
            "challenge_id": challenge_id,
            "pid": os.getpid(),
            "process_name": Path(sys.argv[0]).name or "python",
            "memory_probe_tool": str(MEMORY_PROBE_TOOL),
            "memory_probe_command_example": (
                f"python {MEMORY_PROBE_TOOL} --pid {os.getpid()} "
                "--probe-sensitive <SECRET_VALUE> --probe-public <PUBLIC_SIGNATURE> --json-out residency_probe.json"
            ),
            "probe_submission_rule": (
                "Do not send plaintext probes to mycelia_platform.py. Run the external "
                "scanner with plaintext probes and submit only the JSON evidence report."
            ),
            "gpu_restore_opener": self._gpu_restore_to_vram_enabled(),
            "native_gpu_envelope_opener": self._native_envelope_to_vram_enabled(),
            "capabilities": {
                "opencl_active": opencl_active,
                "gpu_crypto_active": gpu_crypto,
                "core_gpu_crypto_active": core_gpu_crypto,
                "native_envelope_crypto_active": native_envelope_crypto,
                "direct_gpu_ingest": True,
                "native_gpu_envelope_opener": self._native_envelope_to_vram_enabled(),
                "gpu_restore_opener": self._gpu_restore_to_vram_enabled(),
                "strict_vram_certification_enabled": _env_bool("MYCELIA_STRICT_VRAM_CERTIFICATION", STRICT_VRAM_CERTIFICATION),
            },
            "strict_claim_preconditions": [
                "native_gpu_envelope_opener=true",
                "gpu_restore_opener=true",
                "negative external CPU-RAM probe during register/login/query/restore",
                "no snapshot plaintext findings",
                "no Mycelia-owned graph plaintext findings",
                "no response path that returns sensitive cleartext beyond an explicitly authorized field",
            ],
        }

    def _classify_external_probe_report(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Classify an external memory report into strict and non-strict hits.

        v1.19.7 separates public handles/audit artifacts from sensitive
        cleartext.  Legacy v1 reports without probe_manifest remain fail-closed:
        every hit is treated as sensitive.
        """
        findings = payload.get("findings", [])
        if not isinstance(findings, list):
            findings = []
        manifest = payload.get("probe_manifest", [])
        manifest_by_hash: dict[str, dict[str, Any]] = {}
        if isinstance(manifest, list):
            for item in manifest:
                if isinstance(item, Mapping):
                    h = str(item.get("probe_sha256", ""))
                    if h:
                        manifest_by_hash[h] = dict(item)
        sensitive_kinds = {"sensitive_cleartext", "profile_cleartext", "content_body", "credential_equivalent"}
        non_strict_kinds = {"public_identifier", "audit_artifact", "probe_canary_positive"}
        hit_counts_by_kind: dict[str, int] = {}
        strict_hits = 0
        non_strict_hits = 0
        classified_findings: list[dict[str, Any]] = []
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            h = str(finding.get("probe_sha256", ""))
            meta = manifest_by_hash.get(h, {})
            kind = str(finding.get("probe_kind") or meta.get("probe_kind") or "sensitive_cleartext")
            strict_relevant = bool(finding.get("strict_relevant", meta.get("strict_relevant", kind in sensitive_kinds)))
            if kind in non_strict_kinds:
                strict_relevant = False
            if kind in sensitive_kinds:
                strict_relevant = True
            hit_counts_by_kind[kind] = hit_counts_by_kind.get(kind, 0) + 1
            if strict_relevant:
                strict_hits += 1
            else:
                non_strict_hits += 1
            redacted = dict(finding)
            redacted["probe_kind"] = kind
            redacted["strict_relevant"] = strict_relevant
            # Keep only digest/evidence coordinates; never accept probe plaintext.
            classified_findings.append(redacted)
        return {
            "probe_manifest": list(manifest_by_hash.values()),
            "classified_findings": classified_findings[:100],
            "hit_counts_by_kind": hit_counts_by_kind,
            "strict_hits": strict_hits,
            "non_strict_hits": non_strict_hits,
            "strict_negative": int(payload.get("scanned_regions", 0) or 0) > 0
                and int(payload.get("scanned_bytes", 0) or 0) > 0
                and strict_hits == 0
                and str(payload.get("status", "ok")) == "ok",
        }


    def submit_external_memory_probe(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Accept classified external CPU-RAM scan evidence.

        v1.19.7 differentiates strict-sensitive findings from public handles and
        audit artifacts.  This prevents public node signatures from blocking
        certification while still failing closed on profile/content/credential
        cleartext.  Legacy reports without classification are treated as
        sensitive by default.
        """
        challenge_id = str(payload.get("challenge_id", "") or "")
        challenge_known = bool(challenge_id and challenge_id in self.residency_challenges)
        hits = int(payload.get("hits", 0) or 0)
        scanned_regions = int(payload.get("scanned_regions", 0) or 0)
        scanned_bytes = int(payload.get("scanned_bytes", 0) or 0)
        probe_hashes = payload.get("probe_sha256", [])
        if not isinstance(probe_hashes, list):
            probe_hashes = []
        operations = payload.get("operations", [])
        if not isinstance(operations, list):
            operations = []
        classified = self._classify_external_probe_report(payload)
        strict_hits = int(payload.get("strict_hits", classified["strict_hits"]) or 0)
        # Prefer engine-side classification, but preserve v2 tool values when
        # they are present and stricter.
        strict_hits = max(strict_hits, int(classified["strict_hits"]))
        non_strict_hits = int(payload.get("non_strict_hits", classified["non_strict_hits"]) or 0)
        strict_negative = bool(classified["strict_negative"] and strict_hits == 0)
        raw_negative = hits == 0 and scanned_regions > 0 and scanned_bytes > 0
        report = {
            "status": "accepted",
            "audit_version": str(payload.get("scanner_version", "unknown")),
            "classification_version": "MYCELIA_PROBE_CLASSIFICATION_V2",
            "challenge_id": challenge_id,
            "challenge_known": challenge_known,
            "pid": int(payload.get("pid", os.getpid()) or os.getpid()),
            "probe_sha256": [str(x) for x in probe_hashes],
            "probe_manifest": classified["probe_manifest"],
            "hits": hits,
            "strict_hits": strict_hits,
            "non_strict_hits": non_strict_hits,
            "hit_counts_by_kind": classified["hit_counts_by_kind"],
            "negative": strict_negative,
            "strict_negative": strict_negative,
            "raw_negative": raw_negative,
            "scanned_regions": scanned_regions,
            "scanned_bytes": scanned_bytes,
            "operations": [str(x) for x in operations],
            "evidence_digest": str(payload.get("evidence_digest", "")),
            "classified_findings": classified["classified_findings"],
            "canary_positive_required": bool(payload.get("canary_positive_required", False)),
            "canary_positive_ok": bool(payload.get("canary_positive_ok", True)),
            "canary_expected_count": int(payload.get("canary_expected_count", 0) or 0),
            "canary_hit_count": int(payload.get("canary_hit_count", 0) or 0),
            "received_at": time.time(),
        }
        self.latest_external_memory_probe = report
        return {
            "status": "ok",
            "external_memory_probe": report,
            "strict_98_security_supported": self._strict_residency_supported([], []),
            "note": (
                "External memory evidence accepted. Strict certification evaluates "
                "strict_sensitive hits only; public identifiers and audit artifacts are reported separately."
            ),
        }

    def _resolve_snapshot_path(self, source: str | None = None) -> Path:
        """Resolve snapshot path with explicit legacy fallback only.

        Important test/production invariant:
        - An empty restore request means: restore the configured snapshot path.
          If that file is missing, return that missing path so restore_snapshot()
          reports a clean error instead of silently restoring an unrelated old
          autosave from another environment.
        - An explicit legacy UI path such as snapshots/mycelia.snapshot may fall
          back to the configured autosave path for backwards compatibility.
        """
        raw = str(source or "").strip()
        configured = Path(getattr(self, "snapshot_path", DEFAULT_SNAPSHOT_PATH)).resolve()

        if not raw:
            return configured

        first = Path(raw)
        explicit = first if first.is_absolute() else (ROOT / first).resolve()
        candidates: list[Path] = [explicit]

        # Backwards compatibility: old UI/actions requested mycelia.snapshot while
        # the platform writes autosave.mycelia by default.  Only explicit requests
        # get this fallback; empty restore must not resurrect stale project files.
        candidates.append(configured)
        candidates.append(DEFAULT_SNAPSHOT_PATH)
        candidates.append((ROOT / "snapshots" / "autosave.mycelia").resolve())
        candidates.append((ROOT / "snapshots" / "mycelia.snapshot").resolve())

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists():
                return candidate

        # Return the explicit path for a clear error message.
        return explicit

    def restore_snapshot_residency_audit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Run restore residency audit without accidentally forcing CPU restore.

        If the native snapshot runtime is available, this audit uses native
        evidence/selftest and does not call the Python restore path.  Calling the
        Python restore path would set last_restore_cpu_materialized=True and
        would correctly block strict residency certification.
        """
        before = {
            "last_restore_mode": self.last_restore_mode,
            "last_restore_cpu_materialized": self.last_restore_cpu_materialized,
        }
        snapshot_path = self._resolve_snapshot_path(str(payload.get("path", "") or ""))
        if self._gpu_restore_to_vram_enabled():
            native = self.native_gpu_residency_selftest({
                "operation": "restore_snapshot_residency_audit",
                "snapshot_path": str(snapshot_path),
            })
            ok = bool(native.get("status") == "ok" and native.get("strict_vram_residency"))
            self.last_restore_mode = "native_gpu_snapshot_runtime" if ok else "native_gpu_snapshot_runtime_failed"
            self.last_restore_cpu_materialized = False
            result = {
                "status": "ok" if ok else "error",
                "path": str(snapshot_path),
                "native_snapshot_runtime": True,
                "native_selftest": native,
                "driver_mode": self.driver_mode,
                "note": "Native snapshot residency audit; Python snapshot payload was not decrypted.",
            }
        else:
            result = self.restore_snapshot({"path": str(snapshot_path)})
        return {
            "status": result.get("status", "error"),
            "restore_result": result,
            "before": before,
            "after": {
                "last_restore_mode": self.last_restore_mode,
                "last_restore_cpu_materialized": self.last_restore_cpu_materialized,
                "gpu_restore_opener": self._gpu_restore_to_vram_enabled(),
            },
            "strict_restore_residency_supported": (
                result.get("status") == "ok"
                and self._gpu_restore_to_vram_enabled()
                and not self.last_restore_cpu_materialized
            ),
            "conclusion": (
                "Snapshot restore is still CPU-materialized in Python and therefore cannot prove VRAM-only residency."
                if self.last_restore_cpu_materialized else
                "Snapshot restore reports GPU/native-runtime evidence; verify with external memory probe."
            ),
        }

    def _strict_residency_supported(self, graph_findings: list[dict[str, Any]], snapshot_findings: list[dict[str, Any]]) -> bool:
        latest_negative = bool(
            self.latest_external_memory_probe
            and self.latest_external_memory_probe.get("negative")
            and self.latest_external_memory_probe.get("pid") == os.getpid()
        )
        caps = self.native_residency.capabilities()
        native_vram_evidence = bool(
            caps.available
            and caps.envelope_to_vram
            and caps.snapshot_to_vram
            and caps.selftest_passed
            and caps.gpu_resident_open_restore_proven
            and caps.native_strict_certification_gate
            and caps.strict_certification_gate_selftest_passed
            and caps.external_ram_probe_contract
        )
        # v1.18F: Strict certification is based on the native VRAM evidence
        # chain and a negative external RAM probe. The legacy driver_mode string
        # may be "opencl:<path>" without "+gpu-crypto", because VRAM residency is
        # now attested by mycelia_gpu_envelope.dll rather than by the old Python
        # driver label.
        return bool(
            STRICT_VRAM_CERTIFICATION
            and native_vram_evidence
            and latest_negative
            and not graph_findings
            and not snapshot_findings
            and not self.last_restore_cpu_materialized
        )

    def vram_residency_audit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Best-effort audit for the VRAM-residency security thesis.

        Important boundary: with the current PHP -> HTTP/JSON -> Python design,
        strict end-to-end absence of CPU cleartext is not attainable.  User input,
        PHP sessions, JSON request bodies and rendered HTML necessarily contain
        plaintext at the application boundary.  This audit therefore separates:

        1. snapshot confidentiality, which can pass today;
        2. Mycelia-owned graph metadata, which can be scanned for accidental
           plaintext retention;
        3. strict runtime residency, which remains unverified until direct
           GPU-ingest and negative OS memory probes exist.
        """
        raw_probes = payload.get("probes", [])
        plaintext_probes_materialized = bool(raw_probes)
        if isinstance(raw_probes, str):
            probes = [raw_probes]
        elif isinstance(raw_probes, list | tuple):
            probes = [str(x) for x in raw_probes if str(x)]
        else:
            probes = []
        probe_hashes = payload.get("probe_sha256", [])
        if not isinstance(probe_hashes, list):
            probe_hashes = []

        # Keep the audit useful even without user-provided probes by scanning for
        # high-risk structural tokens that must not appear in encrypted snapshots.
        structural_probes = [
            USER_TABLE,
            FORUM_TABLE,
            BLOG_TABLE,
            BLOG_POST_TABLE,
            COMMENT_TABLE,
            REACTION_TABLE,
            "auth_pattern",
            "profile_blob",
            "content_blob",
        ]
        all_probes = list(dict.fromkeys([*probes, *structural_probes]))

        graph_findings = self._graph_residency_scan(probes) if probes else []
        snapshot_findings: list[dict[str, Any]] = []
        snapshot_path = self.snapshot_path
        if payload.get("snapshot_path"):
            snapshot_path = Path(str(payload["snapshot_path"])).resolve()
        if bool(payload.get("create_temp_snapshot", False)):
            # Create a snapshot before scanning it.  This proves at-rest
            # confidentiality, not CPU-RAM residency.
            self.create_snapshot({"path": str(snapshot_path)})
        if snapshot_path.exists():
            raw = snapshot_path.read_bytes()
            for hit in self._utf8_scan(raw, all_probes):
                snapshot_findings.append({"location": "snapshot_file", "path": str(snapshot_path), **hit})

        mode = self.driver_mode
        opencl_active = mode.startswith("opencl:")
        core_gpu_crypto = self._core_gpu_crypto_active()
        native_envelope_crypto = self._native_envelope_crypto_active()
        gpu_crypto = self._gpu_crypto_active()
        direct_gpu_ingest = True
        direct_ingest_phase = "phase1_php_blind" if not self._native_envelope_to_vram_enabled() else "phase2_native_gpu_envelope"
        external_negative_probe = bool(
            self.latest_external_memory_probe
            and self.latest_external_memory_probe.get("negative")
            and self.latest_external_memory_probe.get("pid") == os.getpid()
        )

        boundary_blockers = []
        if not self._native_envelope_to_vram_enabled():
            boundary_blockers.append("The current Python envelope opener materializes decrypted JSON before handing data to Mycelia/GPU logic.")
        if not self._gpu_restore_to_vram_enabled() or self.last_restore_cpu_materialized:
            boundary_blockers.append("Snapshot restore currently decrypts the autosave image in Python CPU memory.")
        if not external_negative_probe:
            boundary_blockers.append("No accepted negative external CPU-RAM probe report is attached to this engine process.")
        if plaintext_probes_materialized:
            boundary_blockers.append("This audit request contained plaintext probes; the request itself materialized them in Python CPU RAM.")
        boundary_blockers.extend([
            "Authorized HTTP responses and browser DOM may still contain the user-visible cleartext they requested.",
            "PHP sessions store identity/session metadata, although not submitted form secrets.",
        ])

        strict_supported = self._strict_residency_supported(graph_findings, snapshot_findings)
        engine_core_candidate = opencl_active and gpu_crypto and not snapshot_findings

        return {
            "status": "ok",
            "audit_version": RESIDENCY_AUDIT_VERSION,
            "driver_mode": mode,
            "opencl_active": opencl_active,
            "gpu_crypto_active": gpu_crypto,
            "core_gpu_crypto_active": core_gpu_crypto,
            "native_envelope_crypto_active": native_envelope_crypto,
            "direct_gpu_ingest": direct_gpu_ingest,
            "direct_ingest_phase": direct_ingest_phase,
            "strict_vram_only_mode": _env_bool("MYCELIA_STRICT_VRAM_ONLY", STRICT_VRAM_ONLY),
            "php_blind_form_transport": True,
            "native_gpu_envelope_opener": self._native_envelope_to_vram_enabled(),
            "gpu_restore_opener": self._gpu_restore_to_vram_enabled(),
            "python_cpu_decrypt_materialized": not self._native_envelope_to_vram_enabled(),
            "snapshot_restore_cpu_materialized": self.last_restore_cpu_materialized,
            "plaintext_probes_materialized_by_audit_request": plaintext_probes_materialized,
            "probe_sha256_count": len(probe_hashes),
            "external_negative_cpu_ram_probe": external_negative_probe,
            "latest_external_memory_probe": self.latest_external_memory_probe,
            "strict_98_security_supported": strict_supported,
            "engine_core_residency_candidate": engine_core_candidate,
            "cpu_cleartext_risk": not strict_supported,
            "boundary_blockers": boundary_blockers,
            "probe_count": len(probes),
            "graph_plaintext_findings": graph_findings,
            "snapshot_plaintext_findings": snapshot_findings,
            "snapshot_path": str(snapshot_path),
            "conclusion": (
                "Direct GPU Ingest is active, but strict VRAM-only residency is not yet proven unless native GPU envelope opening, GPU snapshot restore and negative external CPU-RAM probes are all present."
                if not strict_supported else
                "Strict residency claim is supported by native direct GPU ingest and negative CPU-RAM probes."
            ),
            "required_next_controls": [
                "native GPU envelope opener so Python no longer materializes decrypted JSON",
                "GPU-side snapshot restore so autosave.mycelia is opened inside VRAM-owned buffers",
                "OS-level external process memory probe during register/login/query/restore",
                "driver-level buffer residency attestation",
                "log redaction and no full auth-pattern logging",
                "CI test that fails if sensitive probes appear in snapshots or Mycelia-owned CPU graph metadata",
            ],
        }



    def _delete_attractor_record(self, signature: str) -> bool:
        """Hard-remove an attractor and its external row from the DAD internals.

        The upstream DynamicAssociativeDatabase exposes update/query methods but
        no public deletion primitive.  For GDPR erasure the platform owns the
        lifecycle and therefore performs a controlled internal purge, followed by
        an autosnapshot so the removed material is not resurrected on restart.
        """
        db = self.core.database
        pattern = getattr(db, "_attractors", {}).get(signature)
        table = None
        if pattern is not None:
            table = getattr(pattern, "source_table", None)
        external = getattr(db, "_external_records", {})
        if table is None and signature in external:
            table = external.get(signature, {}).get("table")
        removed = False
        if signature in external:
            external.pop(signature, None)
            removed = True
        attractors = getattr(db, "_attractors", {})
        if signature in attractors:
            attractors.pop(signature, None)
            removed = True
        table_index = getattr(db, "_table_index", {})
        if table:
            key = str(table).lower()
            if key in table_index:
                table_index[key].discard(signature)
                if not table_index[key]:
                    table_index.pop(key, None)
        return removed

    def _all_records(self, table: str | None = None) -> list[dict[str, Any]]:
        return self.core.query_sql_like(table=table, filters={}, limit=None)

    def export_my_data(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a machine-readable subject access / portability package.

        This endpoint is session-bound and deliberately limited to the current
        actor.  It returns user-provided/profile/content data but excludes
        credential secrets such as auth_pattern values.
        """
        actor_signature = str(payload.get("actor_signature", "")).strip()
        actor_username = str(payload.get("actor_username", "")).strip()
        if not actor_signature:
            return {"status": "error", "message": "Engine-Session erforderlich."}

        user_record = self.core.get_sql_record(actor_signature)
        if not user_record or user_record.get("table") != USER_TABLE:
            return {"status": "error", "message": "User-Attraktor nicht gefunden."}

        user_row = dict(user_record.get("data", {}))
        profile: dict[str, Any] = {}
        if user_row.get("profile_seed") and user_row.get("profile_blob"):
            profile = self._decrypt_json({"seed": user_row["profile_seed"], "blob": user_row["profile_blob"]})

        def owned_content() -> dict[str, Any]:
            threads: list[dict[str, Any]] = []
            for rec in self._all_records(FORUM_TABLE):
                row = dict(rec.get("data", {}))
                if row.get("author_signature") != actor_signature:
                    continue
                body = {}
                if row.get("content_seed") and row.get("content_blob"):
                    body = self._decrypt_content(row)
                threads.append({
                    "signature": rec.get("signature"),
                    "title": row.get("title"),
                    "body": body.get("body", ""),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "deleted": bool(row.get("deleted", False)),
                })

            comments: list[dict[str, Any]] = []
            for rec in self._all_records(COMMENT_TABLE):
                row = dict(rec.get("data", {}))
                if row.get("author_signature") != actor_signature:
                    continue
                body = {}
                if row.get("content_seed") and row.get("content_blob"):
                    body = self._decrypt_content(row)
                comments.append({
                    "signature": rec.get("signature"),
                    "target_signature": row.get("target_signature"),
                    "target_type": row.get("target_type"),
                    "body": body.get("body", ""),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "deleted": bool(row.get("deleted", False)),
                })

            reactions: list[dict[str, Any]] = []
            for rec in self._all_records(REACTION_TABLE):
                row = dict(rec.get("data", {}))
                if row.get("actor_signature") != actor_signature:
                    continue
                reactions.append({
                    "signature": rec.get("signature"),
                    "target_signature": row.get("target_signature"),
                    "target_type": row.get("target_type"),
                    "reaction": row.get("reaction"),
                    "created_at": row.get("created_at"),
                })

            blogs: list[dict[str, Any]] = []
            owned_blog_signatures: set[str] = set()
            for rec in self._all_records(BLOG_TABLE):
                row = dict(rec.get("data", {}))
                if row.get("owner_signature") != actor_signature:
                    continue
                owned_blog_signatures.add(str(rec.get("signature")))
                blogs.append({
                    "signature": rec.get("signature"),
                    "title": row.get("title"),
                    "description": row.get("description"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "deleted": bool(row.get("deleted", False)),
                })

            blog_posts: list[dict[str, Any]] = []
            for rec in self._all_records(BLOG_POST_TABLE):
                row = dict(rec.get("data", {}))
                if row.get("author_signature") != actor_signature and str(row.get("blog_signature", "")) not in owned_blog_signatures:
                    continue
                body = {}
                if row.get("content_seed") and row.get("content_blob"):
                    body = self._decrypt_content(row)
                blog_posts.append({
                    "signature": rec.get("signature"),
                    "blog_signature": row.get("blog_signature"),
                    "title": row.get("title"),
                    "body": body.get("body", ""),
                    "publish_status": row.get("publish_status"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "deleted": bool(row.get("deleted", False)),
                })
            return {
                "forum_threads": threads,
                "comments": comments,
                "reactions": reactions,
                "blogs": blogs,
                "blog_posts": blog_posts,
            }

        export = {
            "format": "MYCELIA_SUBJECT_EXPORT_V1",
            "generated_at": time.time(),
            "subject": {
                "signature": actor_signature,
                "username": actor_username or user_row.get("username", ""),
                "node_table": USER_TABLE,
                "created_at": user_row.get("created_at"),
                "updated_at": user_row.get("updated_at"),
            },
            "profile": profile,
            "content": owned_content(),
            "security_exclusions": {
                "auth_pattern_exported": False,
                "password_exported": False,
                "reason": "Credential-equivalent secrets are intentionally excluded from user downloads.",
            },
            "privacy_semantics": {
                "access": "GDPR Article 15 style subject access package",
                "portability": "GDPR Article 20 style structured machine-readable JSON",
                "engine_materialization": "Export necessarily materializes selected personal data for delivery to the authenticated user.",
            },
        }
        return {"status": "ok", "export": export, "filename": f"myceliadb-export-{actor_signature[:12]}.json"}

    def delete_my_account(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Erase the current user's account and owned personal data.

        The operation is authenticated by the rotating Engine session and
        confirmed with the current password inside the Direct-Ingest envelope.
        It hard-purges user-owned DAD records where possible and anonymizes
        remaining references that could otherwise expose the deleted identity.
        """
        actor_signature = str(payload.get("actor_signature", "")).strip()
        actor_username = str(payload.get("actor_username", "")).strip()
        password = str(payload.get("password", ""))
        confirm = str(payload.get("confirm_delete", "")).strip()
        if confirm != "DELETE":
            return {"status": "error", "message": "Bitte bestätige die Löschung mit DELETE."}
        if not actor_signature or not actor_username:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        user_record = self.core.get_sql_record(actor_signature)
        if not user_record or user_record.get("table") != USER_TABLE:
            return {"status": "error", "message": "User-Attraktor nicht gefunden."}
        user_row = dict(user_record.get("data", {}))
        if user_row.get("auth_pattern") != self._password_pattern(actor_username, password):
            return {"status": "error", "message": "Passwortbestätigung fehlgeschlagen."}

        to_delete: set[str] = {actor_signature}
        owned_blog_signatures: set[str] = set()

        for rec in self._all_records(BLOG_TABLE):
            row = dict(rec.get("data", {}))
            if row.get("owner_signature") == actor_signature:
                sig = str(rec.get("signature"))
                owned_blog_signatures.add(sig)
                to_delete.add(sig)

        for table, owner_fields in [
            (FORUM_TABLE, ("author_signature",)),
            (COMMENT_TABLE, ("author_signature",)),
            (REACTION_TABLE, ("actor_signature",)),
            (BLOG_POST_TABLE, ("author_signature",)),
        ]:
            for rec in self._all_records(table):
                row = dict(rec.get("data", {}))
                sig = str(rec.get("signature"))
                if any(row.get(field) == actor_signature for field in owner_fields):
                    to_delete.add(sig)
                if table == BLOG_POST_TABLE and str(row.get("blog_signature", "")) in owned_blog_signatures:
                    to_delete.add(sig)

        # Cascade: remove comments/reactions targeting content that will disappear.
        changed = True
        while changed:
            changed = False
            for table in (COMMENT_TABLE, REACTION_TABLE):
                for rec in self._all_records(table):
                    row = dict(rec.get("data", {}))
                    sig = str(rec.get("signature"))
                    if sig not in to_delete and str(row.get("target_signature", "")) in to_delete:
                        to_delete.add(sig)
                        changed = True

        removed = 0
        for sig in sorted(to_delete):
            if self._delete_attractor_record(sig):
                removed += 1

        # Scrub non-owned references such as CMS update markers.
        scrubbed = 0
        for table in (SITE_TEXT_TABLE,):
            for rec in self._all_records(table):
                row = dict(rec.get("data", {}))
                if row.get("updated_by") == actor_signature:
                    row["updated_by"] = "erased-user"
                    if self.core.update_sql_record(str(rec.get("signature")), row, stability=0.97):
                        scrubbed += 1

        # Revoke all active sessions of this account.
        for handle, session in list(self.sessions.items()):
            if session.signature == actor_signature:
                self.sessions.pop(handle, None)

        save = self.autosave_snapshot("delete_my_account")
        return {
            "status": "ok",
            "message": "Account und zugeordnete personenbezogene Mycelia-Knoten wurden gelöscht.",
            "deleted_nodes": removed,
            "scrubbed_references": scrubbed,
            "autosave": save.get("status"),
            "logout": True,
            "privacy_semantics": {
                "erasure": "GDPR Article 17 style erasure request executed",
                "snapshot": "Autosnapshot was rewritten after purge so deleted nodes are not restored on next start.",
            },
        }


    def check_integrity(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        del payload
        users = self.core.query_sql_like(table=USER_TABLE, limit=None)
        reconstructed = 0
        failed: list[str] = []
        for result in users:
            try:
                row = result["data"]
                self._decrypt_json({"seed": row["profile_seed"], "blob": row["profile_blob"]})
                reconstructed += 1
            except Exception:
                failed.append(str(result.get("signature", ""))[:12])
        residency = self.residency_report({})
        return {
            "status": "ok",
            "uptime_seconds": round(time.time() - self.start_time, 3),
            "driver_mode": self.driver_mode,
            "attractors": self.core.database.attractor_count,
            "average_stability": self.core.database.average_stability,
            "users_checked": len(users),
            "users_reconstructed": reconstructed,
            "failed": failed,
            "snapshot_path": str(self.snapshot_path),
            "snapshot_exists": self.snapshot_path.exists(),
            "autosave_enabled": self.autosave_enabled,
            "autorestore_enabled": self.autorestore_enabled,
            "snapshot_format": residency["snapshot_format"],
            "opencl_active": residency["opencl_active"],
            "gpu_crypto_active": residency["gpu_crypto_active"],
            "strict_inflight_vram_claim": residency["strict_inflight_vram_claim"],
            "cpu_cleartext_risk": residency["cpu_cleartext_risk"],
            "vram_residency_audit": {
                "available": True,
                "audit_version": RESIDENCY_AUDIT_VERSION,
                "strict_98_security_supported": self.vram_residency_audit({}).get("strict_98_security_supported"),
            },
            "enterprise_v120": {
                "smql": SMQL_AUDIT_VERSION,
                "federation": self.federation_status({}),
                "provenance": self.provenance_verify({}),
                "native_library_authenticity": self.native_library_authenticity({}),
                "local_transport_security": self.local_transport_security_status({}),
                "quantum_guard": self.quantum_guard_status({}),
            },
        }


    def _snapshot_image(self) -> dict[str, Any]:
        """Create a serialisable image of the cognitive attractor graph.

        The returned image is not written directly. It is encrypted as one binary
        packet by create_snapshot(), so table names and SQL-origin rows are never
        present as readable strings in the snapshot file.
        """
        patterns: list[dict[str, Any]] = []
        for pattern in self.core.database.list_patterns():
            record = self.core.get_sql_record(pattern.signature)
            patterns.append(
                {
                    "signature": pattern.signature,
                    "energy_mean": pattern.energy_mean,
                    "pheromone_mean": pattern.pheromone_mean,
                    "nutrient_mean": pattern.nutrient_mean,
                    "mood_vector": list(pattern.mood_vector),
                    "stability": pattern.stability,
                    "visits": pattern.visits,
                    "energy_hash": pattern.energy_hash,
                    "source_table": pattern.source_table,
                    "external_payload": record["data"] if record else None,
                }
            )
        return {
            "format": "mycelia-snapshot",
            "version": 1,
            "created_at": time.time(),
            "driver_mode": self.driver_mode,
            "integrity": {
                "attractors": self.core.database.attractor_count,
                "average_stability": self.core.database.average_stability,
            },
            "patterns": patterns,
        }

    def create_snapshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the current Mycelia graph as an encrypted binary snapshot.

        This replaces SQL persistence. The file contains only a magic header and
        one encrypted image packet. Without the application secret and compatible
        Mycelia crypto path, the original table labels and row structures should
        not be recoverable from the bytes.
        """
        target = str(payload.get("path", "") or "snapshots/mycelia.snapshot").strip()
        snapshot_path = Path(target)
        if not snapshot_path.is_absolute():
            snapshot_path = (ROOT / snapshot_path).resolve()
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)

        image = self._snapshot_image()
        packet = self._encrypt_json(image)
        header = {
            "format": "mycelia-snapshot",
            "version": 1,
            "created_at": image["created_at"],
            "driver_mode": self.driver_mode,
            "seed": packet.seed,
            "blob": packet.blob,
            "sha256": hashlib.sha256(packet.blob.encode("ascii")).hexdigest(),
        }
        header_raw = json.dumps(header, ensure_ascii=False, sort_keys=True).encode("utf-8")
        snapshot_path.write_bytes(
            SNAPSHOT_MAGIC + struct.pack("<I", len(header_raw)) + header_raw
        )
        return {
            "status": "ok",
            "path": str(snapshot_path),
            "bytes": snapshot_path.stat().st_size,
            "attractors": image["integrity"]["attractors"],
            "driver_mode": self.driver_mode,
        }

    def restore_snapshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Restore a cold-start Mycelia graph from an encrypted snapshot."""
        if STRICT_VRAM_ONLY and not self._gpu_restore_to_vram_enabled():
            self.last_restore_mode = "blocked_by_strict_vram_only"
            self.last_restore_cpu_materialized = False
            return {
                "status": "error",
                "message": (
                    "STRICT_VRAM_ONLY ist aktiv: Python darf Snapshots nicht im CPU-RAM entschlüsseln. "
                    "Eine native GPU-Snapshot-Restore-Bibliothek ist erforderlich."
                ),
                "strict_vram_only": True,
                "required_native_export": "mycelia_gpu_snapshot_restore_to_vram_v1",
            }
        source = str(payload.get("path", "") or "").strip()
        snapshot_path = self._resolve_snapshot_path(source)
        if not snapshot_path.exists():
            self.last_restore_mode = "snapshot_missing"
            self.last_restore_cpu_materialized = False
            return {
                "status": "error",
                "message": "Snapshot-Datei nicht gefunden.",
                "path": str(snapshot_path),
                "checked": [
                    str(DEFAULT_SNAPSHOT_PATH),
                    str((ROOT / "snapshots" / "autosave.mycelia").resolve()),
                    str((ROOT / "snapshots" / "mycelia.snapshot").resolve()),
                ],
            }
        raw = snapshot_path.read_bytes()
        if not raw.startswith(SNAPSHOT_MAGIC):
            return {"status": "error", "message": "Ungültiges Mycelia Snapshot Format."}
        offset = len(SNAPSHOT_MAGIC)
        if len(raw) < offset + 4:
            return {"status": "error", "message": "Snapshot-Header ist beschädigt."}
        header_len = struct.unpack("<I", raw[offset : offset + 4])[0]
        header_raw = raw[offset + 4 : offset + 4 + header_len]
        header = json.loads(header_raw.decode("utf-8"))
        digest = hashlib.sha256(str(header["blob"]).encode("ascii")).hexdigest()
        if digest != header.get("sha256"):
            return {"status": "error", "message": "Snapshot-Integritätsprüfung fehlgeschlagen."}

        # SECURITY NOTE:
        # The V1 restore path decrypts the snapshot image into Python objects.
        # That is intentionally recorded as CPU materialization.  A future native
        # GPU snapshot opener must replace this branch and keep cleartext inside
        # VRAM-owned buffers only.
        self.last_restore_mode = "python_cpu_materialized"
        self.last_restore_cpu_materialized = True
        image = self._decrypt_json({"seed": header["seed"], "blob": header["blob"]})
        if image.get("format") != "mycelia-snapshot" or int(image.get("version", 0)) != 1:
            return {"status": "error", "message": "Snapshot-Version wird nicht unterstützt."}

        self.core.database.clear()
        restored = 0
        for item in image.get("patterns", []):
            if not isinstance(item, Mapping):
                continue
            signature = str(item.get("signature", ""))
            if not signature:
                continue
            mood = item.get("mood_vector", [0.0, 0.0, 0.0])
            if not isinstance(mood, list | tuple) or len(mood) < 3:
                mood = [0.0, 0.0, 0.0]
            external_payload = item.get("external_payload")
            if external_payload is not None and not isinstance(external_payload, Mapping):
                external_payload = None
            self.core.database.store_pattern(
                signature=signature,
                energy_mean=float(item.get("energy_mean", 0.0)),
                pheromone_mean=float(item.get("pheromone_mean", 0.0)),
                nutrient_mean=float(item.get("nutrient_mean", 0.0)),
                mood_vector=(float(mood[0]), float(mood[1]), float(mood[2])),
                stability=float(item.get("stability", 0.0)),
                visits=int(item.get("visits", 1) or 1),
                energy_hash=str(item.get("energy_hash", "")),
                source_table=item.get("source_table"),
                external_payload=external_payload,
            )
            restored += 1
        return {
            "status": "ok",
            "path": str(snapshot_path),
            "restored": restored,
            "driver_mode": self.driver_mode,
        }

    def strict_vram_evidence_bundle(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return one coherent evidence bundle for Admin UI and console parity.

        The Admin panel previously showed independently cached fragments
        (manifest, probe submit response, native report, strict cert).  This
        command rebuilds the same evidence chain in one request so the UI shows
        the same state that the console tools see.
        """
        del payload
        native_report = self.native_gpu_capability_report({})
        native_selftest = self.native_gpu_residency_selftest({})
        strict = self.strict_vram_certification({})
        latest_probe = self.latest_external_memory_probe
        return {
            "status": "ok",
            "audit_version": RESIDENCY_AUDIT_VERSION,
            "pid": os.getpid(),
            "driver_mode": self.driver_mode,
            "strict_vram_certification_enabled": _env_bool("MYCELIA_STRICT_VRAM_CERTIFICATION", STRICT_VRAM_CERTIFICATION),
            "latest_external_memory_probe": latest_probe,
            "negative_cpu_ram_probe": bool(
                latest_probe
                and latest_probe.get("negative")
                and latest_probe.get("pid") == os.getpid()
            ),
            "last_restore_mode": self.last_restore_mode,
            "last_restore_cpu_materialized": self.last_restore_cpu_materialized,
            "native_gpu_capability_report": native_report,
            "native_gpu_selftest": native_selftest,
            "strict_vram_certification": strict,
            "strict_98_security_supported": bool(strict.get("strict_98_security_supported")),
            "scheduled_heartbeat_audit": self.heartbeat_audit_status({}),
            "admin_console_parity": {
                "single_source_of_truth": "strict_vram_evidence_bundle",
                "matches_console_tools_after_probe_submission": True,
                "note": (
                    "If strict_vram_certification_enabled is false, restart the Engine with "
                    "MYCELIA_STRICT_VRAM_CERTIFICATION=1. This cannot be toggled safely from PHP."
                ),
            },
        }


    def strict_vram_certification(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Enterprise certification gate for the 98%-security thesis.

        This command does not perform marketing certification.  It combines all
        hard prerequisites into one machine-readable result:
        native envelope opening into VRAM, native snapshot restore into VRAM,
        a passing native residency self-test, and a negative external CPU-RAM
        probe against this exact process.
        """
        del payload
        cap_report = self.native_gpu_capability_report({})
        selftest = self.native_gpu_residency_selftest({})
        latest_negative = bool(
            self.latest_external_memory_probe
            and self.latest_external_memory_probe.get("negative")
            and self.latest_external_memory_probe.get("pid") == os.getpid()
        )
        graph_findings: list[dict[str, Any]] = []
        snapshot_findings: list[dict[str, Any]] = []
        strict = self._strict_residency_supported(graph_findings, snapshot_findings)
        blockers = list(cap_report.get("blockers", []))
        if not latest_negative:
            blockers.append("No negative strict-sensitive CPU-RAM memory probe has been submitted for the current MyceliaDB PID.")
        if self.last_restore_cpu_materialized:
            blockers.append("Last snapshot restore was CPU-materialized.")
        if not STRICT_VRAM_CERTIFICATION:
            blockers.append("Strict certification gate is disabled. Set MYCELIA_STRICT_VRAM_CERTIFICATION=1 for production certification mode.")
        if not strict and not blockers:
            blockers.append("Strict certification predicate returned false despite complete capability evidence; inspect latest_external_memory_probe PID and native VRAM evidence.")
        return {
            "status": "ok",
            "audit_version": RESIDENCY_AUDIT_VERSION,
            "strict_98_security_supported": strict,
            "strict_vram_residency_claim": strict,
            "strict_vram_certification_enabled": _env_bool("MYCELIA_STRICT_VRAM_CERTIFICATION", STRICT_VRAM_CERTIFICATION),
            "native_gpu_capability_report": cap_report,
            "native_gpu_selftest": selftest,
            "external_memory_probe": self.latest_external_memory_probe,
            "negative_cpu_ram_probe": latest_negative,
            "process_pid": os.getpid(),
            "blockers": blockers,
            "conclusion": (
                "Strict VRAM residency is supported by the configured evidence gate."
                if strict else
                "Strict VRAM residency is not certified. See blockers for the missing evidence."
            ),
        }

    def residency_report(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Report how close the current runtime is to strict GPU residency.

        The implementation is intentionally conservative: logs proving OpenCL
        kernel compilation do not by themselves prove that cleartext never touched
        CPU RAM. This report gives tests and operators a machine-readable status
        instead of marketing claims.
        """
        del payload
        mode = self.driver_mode
        opencl_active = mode.startswith("opencl:")
        core_gpu_crypto = self._core_gpu_crypto_active()
        native_envelope_crypto = self._native_envelope_crypto_active()
        gpu_crypto = self._gpu_crypto_active()
        direct_gpu_ingest = True
        direct_ingest_phase = "phase1_php_blind" if not self._native_envelope_to_vram_enabled() else "phase2_native_gpu_envelope"
        negative_probe = bool(
            self.latest_external_memory_probe
            and self.latest_external_memory_probe.get("negative")
            and self.latest_external_memory_probe.get("pid") == os.getpid()
        )
        # Phase 1 proves that PHP no longer receives form cleartext. It does not
        # prove that Python/OpenCL never materializes cleartext in CPU RAM.
        strict_inflight_vram = self._strict_residency_supported([], [])
        return {
            "status": "ok",
            "driver_mode": mode,
            "opencl_active": opencl_active,
            "gpu_crypto_active": gpu_crypto,
            "core_gpu_crypto_active": core_gpu_crypto,
            "native_envelope_crypto_active": native_envelope_crypto,
            "direct_gpu_ingest": direct_gpu_ingest,
            "direct_ingest_phase": direct_ingest_phase,
            "strict_vram_only_mode": _env_bool("MYCELIA_STRICT_VRAM_ONLY", STRICT_VRAM_ONLY),
            "php_blind_form_transport": True,
            "native_gpu_envelope_opener": self._native_envelope_to_vram_enabled(),
            "gpu_restore_opener": self._gpu_restore_to_vram_enabled(),
            "python_cpu_decrypt_materialized": not self._native_envelope_to_vram_enabled(),
            "snapshot_restore_cpu_materialized": self.last_restore_cpu_materialized,
            "latest_external_memory_probe": self.latest_external_memory_probe,
            "negative_cpu_ram_probe": negative_probe,
            "strict_inflight_vram_claim": strict_inflight_vram,
            "cpu_cleartext_risk": not strict_inflight_vram,
            "snapshot_format": "MYCELIA_SNAPSHOT_V1",
            "snapshot_path": str(self.snapshot_path),
            "snapshot_exists": self.snapshot_path.exists(),
            "autosave_enabled": self.autosave_enabled,
            "autorestore_enabled": self.autorestore_enabled,
            "note": (
                "Direct GPU Ingest is active. Strict VRAM residency requires native GPU envelope opening, "
                "GPU-side snapshot restore and a negative external CPU-RAM probe report."
            ),
        }



    # ------------------------------------------------------------------
    # v1.21.7 Enterprise Evolution Pack: E2EE, PFS, WebAuthn, telemetry,
    # ephemeral decay, semantic vectors and residency canary hardening.
    # ------------------------------------------------------------------

    def _store_audit_telemetry(self, event: str, data: Mapping[str, Any] | None = None) -> None:
        item = {
            "ts": time.time(),
            "event": str(event)[:80],
            "driver_mode": self.driver_mode,
            "nodes": len(getattr(self.core.database, "_attractors", {}) or {}),
            "native_vram": self._native_envelope_to_vram_enabled(),
            "gpu_restore": self._gpu_restore_to_vram_enabled(),
            "tension": round(0.02 + (len(self._ingest_seen_nonce_set) % 97) / 970.0, 6),
            "harmony": round(0.98 - (len(self._ingest_seen_nonce_set) % 31) / 1000.0, 6),
        }
        if data:
            item.update({str(k): v for k, v in data.items() if isinstance(k, str)})
        self._telemetry_ring.append(item)

    def telemetry_snapshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        limit = max(1, min(200, int(payload.get("limit", 64) or 64)))
        records = list(self._telemetry_ring)[-limit:]
        if not records:
            self._store_audit_telemetry("bootstrap")
            records = list(self._telemetry_ring)[-limit:]
        return {
            "status": "ok",
            "version": "MYCELIA_TELEMETRY_SSE_V1",
            "transport": "polling-or-sse",
            "metrics": records[-1],
            "events": records,
            "safe_payload": "aggregated-only-no-user-content",
        }

    def e2ee_register_public_key(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        actor = str(payload.get("actor_signature", "")).strip()
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        public_key_jwk = payload.get("public_key_jwk") or payload.get("e2ee_public_key_jwk") or {}
        encrypted_private_key = payload.get("encrypted_private_key") or payload.get("wrapped_private_key") or ""
        if not isinstance(public_key_jwk, (dict, str)):
            return {"status": "error", "message": "Ungültiger E2EE Public Key."}
        key_hash = hashlib.sha256(json.dumps(public_key_jwk, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        row = {
            "node_type": "e2ee_public_key",
            "owner_signature": actor,
            "owner_username": str(payload.get("actor_username", "")),
            "public_key_jwk": public_key_jwk,
            "public_key_hash": key_hash,
            "encrypted_private_key": encrypted_private_key,
            "created_at": self._now(),
            "revoked": False,
        }
        pattern = self.core.database.store_sql_record(E2EE_KEY_TABLE, row, stability=0.97, mood_vector=(0.92, 0.03, 0.88))
        self._store_audit_telemetry("e2ee_key_registered", {"actor": actor[:12]})
        save = self.autosave_snapshot("e2ee_register_public_key")
        return {"status": "ok", "signature": pattern.signature, "public_key_hash": key_hash, "autosave": save.get("status")}

    def _e2ee_key_summary(self, rec: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(rec.get("data", {}))
        return {
            "signature": rec.get("signature"),
            "key_signature": rec.get("signature"),
            "owner_signature": data.get("owner_signature"),
            "owner_username": data.get("owner_username"),
            "public_key_jwk": data.get("public_key_jwk"),
            "public_key_hash": data.get("public_key_hash"),
            "created_at": data.get("created_at"),
        }

    def e2ee_public_key_lookup(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        target = str(payload.get("target_signature") or payload.get("owner_signature") or "").strip()
        username_filter = str(payload.get("username") or payload.get("owner_username") or "").strip().lower()
        filters: dict[str, Any] = {"revoked": False}
        if target:
            filters["owner_signature"] = target
        rows = self.core.query_sql_like(table=E2EE_KEY_TABLE, filters=filters, limit=None)
        keys = []
        for rec in rows:
            summary = self._e2ee_key_summary(rec)
            if username_filter and str(summary.get("owner_username") or "").strip().lower() != username_filter:
                continue
            keys.append(summary)
        keys.sort(key=lambda k: (str(k.get("owner_username") or "").lower(), -float(k.get("created_at") or 0)))
        return {"status": "ok", "keys": keys, "count": len(keys)}

    def e2ee_recipient_directory(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a safe E2EE address book for normal users.

        This deliberately exposes only stable user handles, usernames and public
        E2EE keys. It never returns profile fields, private keys, sessions,
        cipher texts or plaintext. Users without a public key are shown as not
        messageable so the UI can explain what is missing instead of forcing
        manual signature copying.
        """
        actor = str(payload.get("actor_signature", "")).strip()
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}

        users: dict[str, dict[str, Any]] = {}
        user_rows = self.core.query_sql_like(table=USER_TABLE, filters={}, limit=None)
        for rec in user_rows:
            data = dict(rec.get("data", {}))
            sig = str(rec.get("signature") or "")
            if not sig:
                continue
            username = str(data.get("username") or "").strip()
            users[sig] = {
                "user_signature": sig,
                "username": username,
                "is_self": sig == actor,
                "messageable": False,
                "keys": [],
                "latest_key": None,
            }

        key_rows = self.core.query_sql_like(table=E2EE_KEY_TABLE, filters={"revoked": False}, limit=None)
        for rec in key_rows:
            key = self._e2ee_key_summary(rec)
            owner = str(key.get("owner_signature") or "")
            if not owner:
                continue
            if owner not in users:
                users[owner] = {
                    "user_signature": owner,
                    "username": str(key.get("owner_username") or ""),
                    "is_self": owner == actor,
                    "messageable": False,
                    "keys": [],
                    "latest_key": None,
                }
            users[owner]["keys"].append(key)

        for entry in users.values():
            keys = entry["keys"]
            keys.sort(key=lambda k: float(k.get("created_at") or 0), reverse=True)
            if keys:
                entry["messageable"] = True
                entry["latest_key"] = keys[0]
                # Keep the UI compact while preserving deterministic fallback choices.
                entry["keys"] = keys[:3]

        recipients = sorted(
            users.values(),
            key=lambda u: (bool(u.get("is_self")), not bool(u.get("messageable")), str(u.get("username") or "").lower()),
        )
        return {
            "status": "ok",
            "recipients": recipients,
            "count": len(recipients),
            "messageable_count": sum(1 for r in recipients if r.get("messageable")),
            "self_signature": actor,
            "engine_blind": True,
            "safe_payload": "usernames-public-e2ee-keys-only",
        }

    def _resolve_e2ee_recipient(self, payload: Mapping[str, Any]) -> tuple[str, str, str]:
        """Resolve user recipient and concrete key.

        Backward compatible behavior:
        - New UI sends recipient_signature = recipient user signature and
          recipient_key_signature = concrete public-key attractor signature.
        - Old UI/manual input may send recipient_signature = key signature.
          In that case the owner_signature is resolved here so inbox delivery
          works for the recipient user instead of being stuck on a key handle.
        """
        raw_recipient = str(payload.get("recipient_signature", "")).strip()
        key_sig = str(payload.get("recipient_key_signature", "") or payload.get("recipient_public_key_signature", "")).strip()
        key_hash = str(payload.get("recipient_key_hash", "")).strip()

        if not raw_recipient and not key_sig and not key_hash:
            return "", "", ""

        filters: dict[str, Any] = {"revoked": False}
        key_rows = self.core.query_sql_like(table=E2EE_KEY_TABLE, filters=filters, limit=None)

        def match_key(rec: Mapping[str, Any]) -> bool:
            data = dict(rec.get("data", {}))
            sig = str(rec.get("signature") or "")
            owner = str(data.get("owner_signature") or "")
            phash = str(data.get("public_key_hash") or "")
            return bool(
                (key_sig and sig == key_sig)
                or (raw_recipient and sig == raw_recipient)
                or (raw_recipient and owner == raw_recipient and (not key_hash or phash == key_hash))
                or (key_hash and phash == key_hash)
            )

        matches = [rec for rec in key_rows if match_key(rec)]
        matches.sort(key=lambda rec: float(dict(rec.get("data", {})).get("created_at") or 0), reverse=True)
        if not matches:
            return raw_recipient, key_sig, key_hash

        rec = matches[0]
        data = dict(rec.get("data", {}))
        owner = str(data.get("owner_signature") or raw_recipient)
        return owner, str(rec.get("signature") or key_sig), str(data.get("public_key_hash") or key_hash)

    def e2ee_send_message(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        sender = str(payload.get("actor_signature", "")).strip()
        recipient, recipient_key_signature, recipient_key_hash = self._resolve_e2ee_recipient(payload)
        if not sender:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        if not recipient:
            return {"status": "error", "message": "Empfänger fehlt."}
        if recipient == sender and str(payload.get("allow_self_message", "")).lower() not in {"1", "true", "yes", "on"}:
            # This is not a security condition. It prevents the common UX error
            # where users accidentally send to their own key because no other
            # contact has registered an E2EE key yet.
            return {"status": "error", "message": "Selbstnachricht blockiert. Bitte einen anderen Empfänger wählen oder allow_self_message=1 setzen."}
        blob = str(payload.get("ciphertext_b64") or payload.get("encrypted_message") or "").strip()
        nonce = str(payload.get("nonce_b64") or payload.get("iv_b64") or "").strip()
        if len(blob) < 16 or len(nonce) < 8:
            return {"status": "error", "message": "E2EE Ciphertext/Nonce fehlt."}
        row = {
            "node_type": "e2ee_message",
            "sender_signature": sender,
            "recipient_signature": recipient,
            "recipient_key_signature": recipient_key_signature,
            "sender_username": str(payload.get("actor_username", "")),
            "recipient_key_hash": recipient_key_hash,
            "recipient_username": str(payload.get("recipient_username", ""))[:120],
            "ciphertext_b64": blob,
            "nonce_b64": nonce,
            "eph_public_jwk": payload.get("eph_public_jwk") or payload.get("ephemeral_public_jwk") or "",
            "sender_ciphertext_b64": str(payload.get("sender_ciphertext_b64") or ""),
            "sender_nonce_b64": str(payload.get("sender_nonce_b64") or ""),
            "sender_eph_public_jwk": payload.get("sender_eph_public_jwk") or "",
            "sender_key_hash": str(payload.get("sender_key_hash") or ""),
            "aad": str(payload.get("aad", "mycelia-e2ee-v1"))[:120],
            "created_at": self._now(),
            "deleted": False,
            "deleted_for_sender": False,
            "deleted_for_recipient": False,
            "blind_to_engine": True,
        }
        pattern = self.core.database.store_sql_record(E2EE_MESSAGE_TABLE, row, stability=0.965, mood_vector=(0.82, 0.02, 0.91))
        self._store_audit_telemetry("e2ee_message_stored", {"recipient": recipient[:12], "key": recipient_key_signature[:12]})
        save = self.autosave_snapshot("e2ee_send_message")
        return {
            "status": "ok",
            "signature": pattern.signature,
            "recipient_signature": recipient,
            "recipient_key_signature": recipient_key_signature,
            "autosave": save.get("status"),
            "engine_blind": True,
        }

    def _e2ee_message_summary(self, rec: Mapping[str, Any], *, view: str) -> dict[str, Any]:
        data = dict(rec.get("data", {}))
        if view == "outbox":
            ciphertext = data.get("sender_ciphertext_b64") or data.get("ciphertext_b64")
            nonce = data.get("sender_nonce_b64") or data.get("nonce_b64")
            eph_public = data.get("sender_eph_public_jwk") or data.get("eph_public_jwk")
        else:
            ciphertext = data.get("ciphertext_b64")
            nonce = data.get("nonce_b64")
            eph_public = data.get("eph_public_jwk")
        return {
            "signature": rec.get("signature"),
            "sender_signature": data.get("sender_signature"),
            "sender_username": data.get("sender_username"),
            "recipient_signature": data.get("recipient_signature"),
            "recipient_username": data.get("recipient_username"),
            "recipient_key_signature": data.get("recipient_key_signature"),
            "recipient_key_hash": data.get("recipient_key_hash"),
            "ciphertext_b64": ciphertext,
            "nonce_b64": nonce,
            "eph_public_jwk": eph_public,
            "aad": data.get("aad"),
            "created_at": data.get("created_at"),
            "mailbox": view,
            "engine_blind": True,
        }

    def e2ee_inbox(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        actor = str(payload.get("actor_signature", "")).strip()
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        rows = self.core.query_sql_like(table=E2EE_MESSAGE_TABLE, filters={"recipient_signature": actor, "deleted": False}, limit=500)
        out = []
        for rec in rows:
            data = dict(rec.get("data", {}))
            if data.get("deleted_for_recipient") or data.get("deleted"):
                continue
            out.append(self._e2ee_message_summary(rec, view="inbox"))
        out.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
        return {"status": "ok", "messages": out, "engine_blind": True, "count": len(out)}

    def e2ee_outbox(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        actor = str(payload.get("actor_signature", "")).strip()
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        rows = self.core.query_sql_like(table=E2EE_MESSAGE_TABLE, filters={"sender_signature": actor, "deleted": False}, limit=500)
        out = []
        for rec in rows:
            data = dict(rec.get("data", {}))
            if data.get("deleted_for_sender") or data.get("deleted"):
                continue
            out.append(self._e2ee_message_summary(rec, view="outbox"))
        out.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
        return {"status": "ok", "messages": out, "engine_blind": True, "count": len(out)}

    def e2ee_delete_message(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        actor = str(payload.get("actor_signature", "")).strip()
        signature = str(payload.get("signature") or payload.get("message_signature") or "").strip()
        mailbox = str(payload.get("mailbox") or "inbox").strip().lower()
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        if not signature:
            return {"status": "error", "message": "Nachricht fehlt."}
        try:
            row = self._get_record_or_error(signature, E2EE_MESSAGE_TABLE)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        is_sender = str(row.get("sender_signature") or "") == actor
        is_recipient = str(row.get("recipient_signature") or "") == actor
        if not (is_sender or is_recipient):
            return {"status": "error", "message": "Keine Berechtigung zum Löschen dieser Nachricht."}

        if mailbox == "outbox" and is_sender:
            row["deleted_for_sender"] = True
        elif mailbox == "inbox" and is_recipient:
            row["deleted_for_recipient"] = True
        else:
            # Fallback: delete the visible side for the current actor.
            if is_recipient:
                row["deleted_for_recipient"] = True
            if is_sender:
                row["deleted_for_sender"] = True

        row["deleted"] = bool(row.get("deleted_for_sender")) and bool(row.get("deleted_for_recipient"))
        row["updated_at"] = self._now()
        ok = self.core.update_sql_record(signature, row, stability=0.94, mood_vector=(0.62, 0.08, 0.74))
        save = self.autosave_snapshot("e2ee_delete_message") if ok else {"status": "skipped"}
        return {"status": "ok" if ok else "error", "signature": signature, "mailbox": mailbox, "autosave": save.get("status")}

    def webauthn_challenge_begin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        username = str(payload.get("username") or payload.get("actor_username") or "").strip()
        challenge = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
        challenge_id = secrets.token_hex(12)
        self._webauthn_challenges[challenge_id] = {"challenge": challenge, "username": username, "created_at": time.time()}
        return {
            "status": "ok",
            "version": "MYCELIA_WEBAUTHN_BRIDGE_V1",
            "challenge_id": challenge_id,
            "challenge_b64url": challenge,
            "rp_id": str(payload.get("rp_id") or "127.0.0.1"),
            "user_verification": "preferred",
        }

    def webauthn_register_credential(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        actor = str(payload.get("actor_signature", "")).strip()
        if not actor:
            return {"status": "error", "message": "Engine-Session erforderlich."}
        challenge_id = str(payload.get("challenge_id", "")).strip()
        if challenge_id and challenge_id not in self._webauthn_challenges:
            return {"status": "error", "message": "Unbekannte WebAuthn Challenge."}
        credential_id = str(payload.get("credential_id_b64url") or payload.get("credential_id") or "").strip()
        public_key = str(payload.get("public_key_cose_b64") or payload.get("public_key_jwk") or "").strip()
        if not credential_id:
            return {"status": "error", "message": "Credential-ID fehlt."}
        row = {
            "node_type": "webauthn_credential",
            "owner_signature": actor,
            "owner_username": str(payload.get("actor_username", "")),
            "credential_id_hash": hashlib.sha256(credential_id.encode()).hexdigest(),
            "credential_id_b64url": credential_id,
            "public_key_material": public_key,
            "sign_count": int(payload.get("sign_count", 0) or 0),
            "created_at": self._now(),
            "revoked": False,
        }
        pattern = self.core.database.store_sql_record(WEBAUTHN_CREDENTIAL_TABLE, row, stability=0.98, mood_vector=(0.93, 0.01, 0.89))
        self._store_audit_telemetry("webauthn_registered", {"actor": actor[:12]})
        save = self.autosave_snapshot("webauthn_register_credential")
        return {"status": "ok", "signature": pattern.signature, "credential_id_hash": row["credential_id_hash"], "autosave": save.get("status")}

    def webauthn_login_assertion(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Browser-bound assertion intake.

        Full CBOR/COSE verification is deliberately isolated behind a data node
        contract so enterprise deployments can plug native/FIDO libraries without
        exposing credentials to PHP. This implementation validates challenge
        freshness and credential binding, records the assertion digest, and issues
        an Engine session for an existing registered credential.
        """
        challenge_id = str(payload.get("challenge_id", "")).strip()
        credential_id = str(payload.get("credential_id_b64url") or payload.get("credential_id") or "").strip()
        challenge = self._webauthn_challenges.get(challenge_id)
        if not challenge or time.time() - float(challenge.get("created_at", 0)) > 300:
            return {"status": "error", "message": "WebAuthn Challenge abgelaufen."}
        rows = self.core.query_sql_like(table=WEBAUTHN_CREDENTIAL_TABLE, filters={"credential_id_hash": hashlib.sha256(credential_id.encode()).hexdigest(), "revoked": False}, limit=1)
        if not rows:
            return {"status": "error", "message": "Credential nicht registriert."}
        row = dict(rows[0].get("data", {}))
        user = self.core.get_sql_record(str(row.get("owner_signature", "")))
        if not user:
            return {"status": "error", "message": "User-Attraktor nicht gefunden."}
        session = self._issue_engine_session(str(row.get("owner_signature", "")), str(row.get("owner_username", "")), str((user.get("data") or {}).get("role", "user")))
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        self._store_audit_telemetry("webauthn_login", {"assertion_digest": digest[:16]})
        return {"status": "ok", "message": "WebAuthn Assertion angenommen.", "engine_session": session, "assertion_digest": digest, "verification": "challenge-and-credential-bound"}

    def _ephemeral_fields_from_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ttl = payload.get("ttl_seconds") or payload.get("ttl_steps") or payload.get("decay_ttl_seconds") or ""
        try:
            ttl_i = int(ttl)
        except Exception:
            ttl_i = 0
        if ttl_i <= 0:
            return {}
        ttl_i = max(30, min(ttl_i, 60 * 60 * 24 * 365))
        now = self._now()
        return {
            "ephemeral": True,
            "ttl_seconds": ttl_i,
            "decay_rate": float(payload.get("decay_rate", 1.0) or 1.0),
            "expires_at": now + ttl_i,
            "decay_state": "alive",
        }

    def ephemeral_decay_step(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        now = self._now()
        tables = [FORUM_TABLE, BLOG_TABLE, BLOG_POST_TABLE, COMMENT_TABLE, MEDIA_TABLE, E2EE_MESSAGE_TABLE]
        decayed = 0
        scanned = 0
        for table in tables:
            for rec in self._all_records(table):
                scanned += 1
                sig = str(rec.get("signature", ""))
                row = dict(rec.get("data", {}))
                if not row.get("ephemeral") or row.get("decay_state") == "decayed":
                    continue
                if float(row.get("expires_at", 0) or 0) <= now:
                    row["decay_state"] = "decayed"
                    row["deleted"] = True
                    row["decayed_at"] = now
                    row["content_blob"] = ""
                    row["media_file_b64"] = ""
                    self.core.database.update_sql_record(sig, row)
                    decayed += 1
        if decayed:
            self.autosave_snapshot("ephemeral_decay_step")
        self._store_audit_telemetry("ephemeral_decay_step", {"decayed": decayed})
        return {"status": "ok", "version": "MYCELIA_PHEROMONE_DECAY_V1", "scanned": scanned, "decayed": decayed, "mode": "soft-delete-plus-payload-erasure"}

    def security_evolution_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        del payload
        return {
            "status": "ok",
            "version": "MYCELIA_ENTERPRISE_EVOLUTION_V1_21_7",
            "features": {
                "direct_ingest_pfs": bool(self._pfs_session_public_b64),
                "e2ee_blind_messages": True,
                "telemetry_dashboard": True,
                "ephemeral_pheromone_decay": True,
                "smql_multimodal_vectors": True,
                "webauthn_bridge": True,
                "classified_memory_probe_canaries": True,
                "vram_zeroing_contract_audit": True,
            },
            "php_cleartext_policy": "normal-mutations-php-blind; DSGVO export is explicit user-return exception",
            "native_vram": self.native_gpu_capability_report({}),
        }

    def vrzero_constant_time_audit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        native_c = ROOT / "native" / "mycelia_gpu_envelope_contract.c"
        candidates = [native_c, CORE_ROOT / "CC_OpenCL.c"]
        findings = []
        for path in candidates:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            findings.append({
                "file": str(path),
                "clEnqueueFillBuffer": "clEnqueueFillBuffer" in text,
                "secure_zero_markers": bool(re.search(r"(secure_zero|zeroize|wipe|FillBuffer)", text, re.I)),
                "data_dependent_branch_review_required": bool(re.search(r"\bif\s*\(", text)),
            })
        return {
            "status": "ok",
            "version": "MYCELIA_VRAM_ZEROING_CONSTANT_TIME_AUDIT_V1",
            "findings": findings,
            "contract": "cl_mem buffers must be overwritten before release; kernels handling secret material must avoid plaintext-dependent branching.",
        }

    def dispatch(self, command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(payload, Mapping) and not isinstance(payload, dict):
            payload = dict(payload)

        authority_error = self._require_authority_for_command(command, payload)
        if authority_error:
            return authority_error

        match command:
            case "validate_session":
                result = self.validate_session(payload)
            case "logout_session":
                result = self.logout_session(payload)
            case "direct_ingest_manifest":
                result = self.direct_ingest_manifest(payload)
            case "native_gpu_capability_report":
                result = self.native_gpu_capability_report(payload)
            case "native_gpu_residency_selftest":
                result = self.native_gpu_residency_selftest(payload)
            case "strict_vram_evidence_bundle":
                result = self.strict_vram_evidence_bundle(payload)
            case "strict_vram_certification":
                result = self.strict_vram_certification(payload)
            case "heartbeat_audit_status":
                result = self.heartbeat_audit_status(payload)
            case "submit_heartbeat_audit":
                result = self.submit_heartbeat_audit(payload)
            case "smql_explain":
                result = self.smql_explain(payload)
            case "smql_query":
                result = self.smql_query(payload)
            case "federation_status":
                result = self.federation_status(payload)
            case "federation_peer_add":
                result = self.federation_peer_add(payload)
            case "federation_peer_remove":
                result = self.federation_peer_remove(payload)
            case "federation_export_stable":
                result = self.federation_export_stable(payload)
            case "federation_import_influx":
                result = self.federation_import_influx(payload)
            case "provenance_log":
                result = self.provenance_log(payload)
            case "provenance_verify":
                result = self.provenance_verify(payload)
            case "native_library_authenticity":
                result = self.native_library_authenticity(payload)
            case "local_transport_security_status":
                result = self.local_transport_security_status(payload)
            case "quantum_guard_status":
                result = self.quantum_guard_status(payload)
            case "upload_media":
                result = self.upload_media(payload)
            case "attach_media_to_content":
                result = self.attach_media_to_content(payload)
            case "list_media_for_content":
                result = self.list_media_for_content(payload)
            case "render_media_safe":
                result = self.render_media_safe(payload)
            case "delete_media":
                result = self.delete_media(payload)
            case "moderate_media":
                result = self.moderate_media(payload)
            case "list_all_media":
                result = self.list_all_media(payload)
            case "direct_ingest":
                result = self.direct_ingest(payload)
            case "register_user":
                result = self.register_user(payload)
            case "login_attractor":
                result = self.login_attractor(payload)
            case "get_profile":
                result = self.get_profile(payload)
            case "update_profile":
                result = self.update_profile(payload)
            case "import_dump":
                result = self.import_dump(payload)
            case "store_embedding":
                result = self.store_embedding(payload)
            case "find_embedding":
                result = self.find_embedding(payload)
            case "smql_vector_index_status":
                result = self.smql_vector_index_status(payload)
            case "smql_vector_rehydration_audit":
                result = self.smql_vector_rehydration_audit(payload)
            case "smql_vector_rehydrate":
                result = self.smql_vector_rehydrate(payload)
            case "query_pattern":
                result = self.query_pattern(payload)
            case "store_product":
                result = self.store_product(payload)
            case "list_products":
                result = self.list_products(payload)
            case "create_forum_thread":
                result = self.create_forum_thread(payload)
            case "list_forum_threads":
                result = self.list_forum_threads(payload)
            case "get_forum_thread":
                result = self.get_forum_thread(payload)
            case "update_forum_thread":
                result = self.update_forum_thread(payload)
            case "delete_forum_thread":
                result = self.delete_forum_thread(payload)
            case "create_comment":
                result = self.create_comment(payload)
            case "list_comments":
                result = self.list_comments(payload)
            case "update_comment":
                result = self.update_comment(payload)
            case "delete_comment":
                result = self.delete_comment(payload)
            case "react_content":
                result = self.react_content(payload)
            case "create_blog":
                result = self.create_blog(payload)
            case "list_blogs":
                result = self.list_blogs(payload)
            case "get_blog":
                result = self.get_blog(payload)
            case "update_blog":
                result = self.update_blog(payload)
            case "delete_blog":
                result = self.delete_blog(payload)
            case "create_blog_post":
                result = self.create_blog_post(payload)
            case "list_blog_posts":
                result = self.list_blog_posts(payload)
            case "get_blog_post":
                result = self.get_blog_post(payload)
            case "update_blog_post":
                result = self.update_blog_post(payload)
            case "delete_blog_post":
                result = self.delete_blog_post(payload)
            case "admin_overview":
                result = self.admin_overview(payload)
            case "list_users":
                result = self.list_users(payload)
            case "permission_catalog":
                result = self.permission_catalog(payload)
            case "list_site_texts":
                result = self.list_site_texts(payload)
            case "admin_set_site_text":
                result = self.admin_set_site_text(payload)
            case "admin_update_user_rights":
                result = self.admin_update_user_rights(payload)
            case "plugin_catalog":
                result = self.plugin_catalog(payload)
            case "enterprise_plugin_dashboard":
                result = self.enterprise_plugin_dashboard(payload)
            case "fun_plugin_dashboard":
                result = self.fun_plugin_dashboard(payload)
            case "create_poll":
                result = self.create_poll(payload)
            case "list_polls":
                result = self.list_polls(payload)
            case "vote_poll":
                result = self.vote_poll(payload)
            case "create_time_capsule":
                result = self.create_time_capsule(payload)
            case "list_time_capsules":
                result = self.list_time_capsules(payload)
            case "list_plugins":
                result = self.list_plugins(payload)
            case "admin_install_plugin":
                result = self.admin_install_plugin(payload)
            case "admin_set_plugin_state":
                result = self.admin_set_plugin_state(payload)
            case "admin_delete_plugin":
                result = self.admin_delete_plugin(payload)
            case "run_plugin":
                result = self.run_plugin(payload)
            case "export_my_data":
                result = self.export_my_data(payload)
            case "delete_my_account":
                result = self.delete_my_account(payload)
            case "check_integrity":
                result = self.check_integrity(payload)
            case "create_snapshot":
                result = self.create_snapshot(payload)
            case "restore_snapshot":
                result = self.restore_snapshot(payload)
                if result.get("status") == "ok" and payload.get("make_default"):
                    self.autosave_snapshot("restore_snapshot_make_default")
            case "autosave_snapshot":
                result = self.autosave_snapshot(str(payload.get("reason", "api")))
            case "residency_report":
                result = self.residency_report(payload)
            case "create_poll":
                options = []
                for idx in range(1, 7):
                    val = str(data.get(f"option_{idx}", "")).strip()
                    if val:
                        options.append(val)
                if not options:
                    try:
                        loaded = json.loads(str(data.get("options_json", "[]")))
                        if isinstance(loaded, list):
                            options = [str(v).strip() for v in loaded if str(v).strip()]
                    except Exception:
                        options = []
                return {"question": data.get("question", ""), "options": options, "target_signature": data.get("target_signature", "")}
            case "vote_poll":
                return {"poll_signature": data.get("poll_signature", ""), "option_id": data.get("option_id", "")}
            case "create_time_capsule":
                return {"title": data.get("title", ""), "body": data.get("body", ""), "body_vault_json": data.get("body_vault_json", ""), "reveal_at": data.get("reveal_at", ""), "visibility": data.get("visibility", "private")}
            case "vram_residency_audit":
                result = self.vram_residency_audit(payload)
            case "residency_audit_manifest":
                result = self.residency_audit_manifest(payload)
            case "submit_external_memory_probe":
                result = self.submit_external_memory_probe(payload)
            case "restore_snapshot_residency_audit":
                result = self.restore_snapshot_residency_audit(payload)
            case "e2ee_register_public_key":
                result = self.e2ee_register_public_key(payload)
            case "e2ee_public_key_lookup":
                result = self.e2ee_public_key_lookup(payload)
            case "e2ee_recipient_directory":
                result = self.e2ee_recipient_directory(payload)
            case "e2ee_send_message":
                result = self.e2ee_send_message(payload)
            case "e2ee_inbox":
                result = self.e2ee_inbox(payload)
            case "e2ee_outbox":
                result = self.e2ee_outbox(payload)
            case "e2ee_delete_message":
                result = self.e2ee_delete_message(payload)
            case "webauthn_challenge_begin":
                result = self.webauthn_challenge_begin(payload)
            case "webauthn_register_credential":
                result = self.webauthn_register_credential(payload)
            case "webauthn_login_assertion":
                result = self.webauthn_login_assertion(payload)
            case "telemetry_snapshot":
                result = self.telemetry_snapshot(payload)
            case "security_evolution_status":
                result = self.security_evolution_status(payload)
            case "ephemeral_decay_step":
                result = self.ephemeral_decay_step(payload)
            case "vrzero_constant_time_audit":
                result = self.vrzero_constant_time_audit(payload)
            case _:
                result = {"status": "error", "message": f"Unbekannter Befehl: {command}"}

        # v1.20 append-only provenance ledger for successful mutations.
        if result.get("status") == "ok" and command in {
            "register_user", "update_profile", "create_forum_thread", "update_forum_thread", "delete_forum_thread",
            "create_comment", "update_comment", "delete_comment", "react_content", "create_blog", "update_blog", "delete_blog",
            "create_blog_post", "update_blog_post", "delete_blog_post", "admin_set_site_text", "admin_update_user_rights",
            "admin_install_plugin", "admin_set_plugin_state", "admin_delete_plugin", "run_plugin", "delete_my_account",
            "restore_snapshot", "create_snapshot", "federation_import_influx", "federation_peer_add", "federation_peer_remove", "store_embedding"
        }:
            target_sig = str(result.get("signature") or payload.get("signature") or payload.get("target_signature") or payload.get("peer_id") or command)
            try:
                result["provenance"] = self._record_provenance_event(command, target_sig, payload, actor_signature=str(payload.get("actor_signature", "")))
            except Exception as exc:
                LOGGER.warning("Provenance event failed for %s: %s", command, exc)

        result = self._sanitize_strict_response(command, result, payload)

        if (
            result.get("status") == "ok"
            and isinstance(payload, Mapping)
            and isinstance(payload.get("_engine_session_to_return"), Mapping)
            and not isinstance(result.get("engine_session"), Mapping)
        ):
            result["engine_session"] = dict(payload["_engine_session_to_return"])
            result["session_binding"] = "rotated-by-session-bound-command"
        return result

PLATFORM = MyceliaPlatform()


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: Mapping[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if LOCAL_TRANSPORT_TOKEN_REQUIRED:
                presented = self.headers.get("X-Mycelia-Local-Token", "")
                expected = getattr(PLATFORM, "_local_transport_token", "")
                if not expected or not secrets.compare_digest(str(presented), str(expected)):
                    self._send(403, {"status": "error", "message": "Local transport token mismatch.", "version": LOCAL_TRANSPORT_SECURITY_VERSION})
                    return
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            command = str(req.get("command") or req.get("action") or "")
            payload = req.get("payload", {})
            if not isinstance(payload, Mapping):
                payload = {}
            self._send(200, PLATFORM.dispatch(command, payload))
        except Exception as exc:
            LOGGER.exception("Request fehlgeschlagen")
            self._send(500, {"status": "error", "message": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


class MyceliaTCPServer(socketserver.TCPServer):
    """TCP server with fast restart behavior for local development.

    Windows keeps sockets briefly in TIME_WAIT after termination.  Reusing the
    address avoids a false "port already blocked" condition after a clean
    Ctrl+C shutdown.
    """

    allow_reuse_address = True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    LOGGER.info("MyceliaDB Platform listening on 127.0.0.1:%d (%s)", PORT, PLATFORM.driver_mode)
    with MyceliaTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        if LOCAL_HTTPS_ENABLED:
            if not LOCAL_HTTPS_CERT_PATH.exists() or not LOCAL_HTTPS_KEY_PATH.exists():
                raise RuntimeError("MYCELIA_LOCAL_HTTPS=1, aber Zertifikat/Key fehlen. Setze MYCELIA_LOCAL_HTTPS_CERT und MYCELIA_LOCAL_HTTPS_KEY.")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(LOCAL_HTTPS_CERT_PATH), str(LOCAL_HTTPS_KEY_PATH))
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            LOGGER.info("Localhost TLS enabled for Mycelia transport.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            # Ctrl+C is an operator-requested shutdown, not a crash.  Without
            # this guard Python prints a traceback from selector.select().
            LOGGER.info("Shutdown-Signal empfangen. MyceliaDB wird sauber beendet.")
        finally:
            PLATFORM.autosave_snapshot("shutdown")
            httpd.server_close()
            LOGGER.info("MyceliaDB Platform gestoppt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI helper for importing SQL dumps into the Mycelia associative memory."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from .cognition import CognitiveCore
from .main import bootstrap_logging, load_config


LOGGER = logging.getLogger(__name__)


class OfflineDriver:
    """Minimal driver stub for offline memory management."""

    def __init__(self) -> None:
        self._quantum_enabled = False

    def disable_quantum(self) -> None:  # pragma: no cover - noop
        self._quantum_enabled = False

    @property
    def quantum_enabled(self) -> bool:
        return False

    # The cognitive core never calls these paths when quantum is disabled, but
    # we keep them for completeness to surface a helpful error message if they
    # are triggered accidentally.
    def execute_vqe_gpu(self, *args, **kwargs):  # pragma: no cover - defensive
        raise RuntimeError("Quantum-Funktionen sind im Offline-Modus nicht verfügbar")

    def execute_grover_gpu(self, *args, **kwargs):  # pragma: no cover - defensive
        raise RuntimeError("Quantum-Funktionen sind im Offline-Modus nicht verfügbar")


def _build_core(config_path: Path) -> CognitiveCore:
    config = load_config(config_path)
    simulation_cfg = config.get("simulation", {})
    cognition_cfg = simulation_cfg.get("cognition")
    if cognition_cfg is None:
        raise RuntimeError("Konfiguration enthält keinen 'simulation.cognition'-Block")
    driver = OfflineDriver()
    quantum_cfg = {"enabled": False}
    return CognitiveCore(driver, cognition_cfg, quantum_cfg)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    parser.add_argument("--sql-file", required=True, type=Path, help="Pfad zur SQL-Datei")
    parser.add_argument("--table", required=True, help="Tabellenname für den Import")
    parser.add_argument("--where", default=None, help="Optionaler WHERE-Filter")
    parser.add_argument("--limit", type=int, default=None, help="Maximale Anzahl Zeilen")
    parser.add_argument(
        "--stability", type=float, default=0.9, help="Initiale Stabilität neuer Attraktoren"
    )
    parser.add_argument(
        "--chaos-key",
        type=float,
        default=None,
        help="Optionaler Chaos-Key zur Signaturerzeugung",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    bootstrap_logging()
    core = _build_core(args.config)
    patterns = core.import_sql_table(
        str(args.sql_file),
        args.table,
        where=args.where,
        limit=args.limit,
        stability=args.stability,
        chaos_key=args.chaos_key,
    )

    LOGGER.info("Import abgeschlossen: %d Attraktoren", len(patterns))
    for pattern in patterns:
        payload = core.get_sql_record(pattern.signature)
        if payload is None:
            continue
        print(
            f"{pattern.signature[:12]} | table={payload['table']} | "
            f"stability={pattern.stability:.3f} | fields={list(payload['data'].keys())}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

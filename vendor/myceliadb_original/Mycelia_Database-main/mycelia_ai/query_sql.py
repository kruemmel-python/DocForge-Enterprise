"""CLI helper for querying SQL-backed attractors."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Dict, Sequence

from .cognition import CognitiveCore
from .import_sql import OfflineDriver
from .main import bootstrap_logging, load_config


LOGGER = logging.getLogger(__name__)


def _build_core(config_path: Path) -> CognitiveCore:
    config = load_config(config_path)
    simulation_cfg = config.get("simulation", {})
    cognition_cfg = simulation_cfg.get("cognition")
    if cognition_cfg is None:
        raise RuntimeError("Konfiguration enthält keinen 'simulation.cognition'-Block")
    driver = OfflineDriver()
    quantum_cfg = {"enabled": False}
    return CognitiveCore(driver, cognition_cfg, quantum_cfg)


def _parse_filters(filter_args: Sequence[str]) -> Dict[str, object]:
    filters: Dict[str, object] = {}
    for entry in filter_args:
        if "=" not in entry:
            raise ValueError(f"Filter muss das Format key=value haben: {entry}")
        key, value = entry.split("=", 1)
        filters[key] = value
    return filters


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    parser.add_argument("--sql-file", type=Path, default=None, help="Optionaler Import vor der Abfrage")
    parser.add_argument("--table", default=None, help="Tabellenname für Import oder Filter")
    parser.add_argument("--where", default=None, help="WHERE-Filter für den optionalen Import")
    parser.add_argument("--limit", type=int, default=20, help="Maximale Anzahl Ergebnisse")
    parser.add_argument(
        "--filter",
        dest="filters",
        action="append",
        default=[],
        help="Feldfilter im Format key=value (mehrfach nutzbar)",
    )
    parser.add_argument("--cue", default=None, help="Optionaler assoziativer Such-Cue")
    parser.add_argument(
        "--intensity",
        type=float,
        default=1.0,
        help="Intensität für assoziative Abfragen",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    bootstrap_logging()
    core = _build_core(args.config)

    if args.sql_file is not None:
        if args.table is None:
            raise RuntimeError("Für den Import muss '--table' gesetzt sein")
        core.import_sql_table(
            str(args.sql_file),
            args.table,
            where=args.where,
            limit=args.limit,
        )

    results: list[Dict[str, object]]
    if args.cue:
        results = core.associative_sql_query(
            args.cue, intensity=args.intensity, limit=args.limit
        )
    else:
        filters = _parse_filters(args.filters)
        results = core.query_sql_like(
            table=args.table,
            filters=filters if filters else None,
            limit=args.limit,
        )

    if not results:
        LOGGER.info("Keine passenden Datensätze gefunden")
        return 0

    for record in results:
        data_preview = ", ".join(f"{key}={value}" for key, value in record["data"].items())
        print(
            f"{record['signature'][:12]} | table={record['table']} | "
            f"stability={record['stability']:.3f} | visits={record['visits']} | {data_preview}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

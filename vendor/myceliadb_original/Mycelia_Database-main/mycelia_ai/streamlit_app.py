"""Streamlit portal that imports SQL dumps and queries the Mycelia memory."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping

# Allow ``streamlit run mycelia_ai/streamlit_app.py`` without installing the
# package by injecting the repository root onto ``sys.path`` when executed as a
# loose script.
if __package__ is None or __package__ == "":
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

import streamlit as st

from mycelia_ai.cognition.dynamic_database import DynamicAssociativeDatabase
from mycelia_ai.io import sql_importer


def _get_database() -> DynamicAssociativeDatabase:
    if "_mycelia_database" not in st.session_state:
        st.session_state["_mycelia_database"] = DynamicAssociativeDatabase()
    return st.session_state["_mycelia_database"]


def _save_uploaded_file(upload) -> Path:
    temp_dir = Path(st.session_state.setdefault("_mycelia_upload_dir", tempfile.mkdtemp()))
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / upload.name
    file_path.write_bytes(upload.getvalue())
    return file_path


def _parse_filter_expression(expression: str) -> tuple[Dict[str, object], List[str]]:
    filters: Dict[str, object] = {}
    errors: List[str] = []
    if not expression:
        return filters, errors
    for clause in expression.split(","):
        clause = clause.strip()
        if not clause:
            continue
        if "=" not in clause:
            errors.append(f"Ungültiger Filter '{clause}'; Format 'spalte=wert' erwartet.")
            continue
        key, value = clause.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            errors.append(f"Fehlender Spaltenname in Filter '{clause}'.")
            continue
        normalized = _coerce_value(value)
        filters[key] = normalized
    return filters, errors


def _coerce_value(value: str) -> object:
    lowered = value.lower()
    if lowered in {"null", "none"}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _parse_mood_vector(payload: str) -> tuple[float, float, float] | None:
    if not payload.strip():
        return None
    tokens = [token.strip() for token in payload.split(",") if token.strip()]
    if len(tokens) != 3:
        return None
    try:
        return tuple(float(token) for token in tokens)  # type: ignore[return-value]
    except ValueError:
        return None


def _render_sidebar(database: DynamicAssociativeDatabase) -> None:
    with st.sidebar:
        st.header("Speicherstatus")
        st.metric("Attraktoren", database.attractor_count)
        st.metric("SQL-Datensätze", database.external_record_count)
        st.metric("Ø Stabilität", f"{database.average_stability:.3f}")
        st.metric("Rauschfaktor", f"{database.noise_factor:.3f}")
        if st.button("Gedächtnis löschen"):
            database.clear()
            st.experimental_rerun()
        st.divider()
        active_db = sql_importer.active_database_path()
        if active_db:
            st.caption(f"Aktive SQLite-Datei: {active_db}")
        else:
            st.caption("Noch kein SQL-Dump importiert.")


def _display_results(title: str, rows: Iterable[Mapping[str, object]]) -> None:
    rows = list(rows)
    st.subheader(title)
    if not rows:
        st.info("Keine Ergebnisse gefunden.")
        return
    formatted = []
    for row in rows:
        formatted.append(
            {
                "Signature": row.get("signature"),
                "Tabelle": row.get("table"),
                "Stabilität": row.get("stability"),
                "Besuche": row.get("visits"),
                "Daten": json.dumps(row.get("data"), ensure_ascii=False),
            }
        )
    st.dataframe(formatted, use_container_width=True)


def _import_tables(
    database: DynamicAssociativeDatabase,
    selected_tables: List[str],
    where_filter: str | None,
    limit: int,
    stability: float,
    mood_vector: tuple[float, float, float] | None,
    chaos_key: float | None,
) -> list[Mapping[str, object]]:
    imported: list[Mapping[str, object]] = []
    for table in selected_tables:
        rows = list(sql_importer.fetch_rows(table, where=where_filter or None, limit=limit))
        for row in rows:
            pattern = database.store_sql_record(
                table,
                row,
                stability=stability,
                mood_vector=mood_vector,
                chaos_key=chaos_key,
            )
            imported.append(
                {
                    "signature": pattern.signature,
                    "table": table,
                    "fields": list(row.keys()),
                    "stability": pattern.stability,
                }
            )
    return imported


def _filter_by_tables(rows: Iterable[Mapping[str, object]], tables: List[str]) -> List[Mapping[str, object]]:
    if not tables:
        return list(rows)
    normalized = {table.lower() for table in tables}
    return [row for row in rows if (row.get("table") or "").lower() in normalized]


def main() -> None:
    st.set_page_config(page_title="Project Mycelia SQL Bridge", layout="wide")
    st.title("Project Mycelia – SQL Bridge")
    database = _get_database()
    _render_sidebar(database)

    tab_import, tab_query = st.tabs(["SQL-Import", "Abfragen"])

    with tab_import:
        st.subheader("SQL-Dump hochladen")
        uploaded = st.file_uploader("SQL-Datei auswählen", type=["sql", "txt", "dump"])
        table_options: List[str] = st.session_state.get("_mycelia_tables", [])
        if uploaded is not None and st.button("Dump einlesen", type="primary"):
            file_path = _save_uploaded_file(uploaded)
            sql_importer.import_sql_file(str(file_path))
            tables = sql_importer.list_tables()
            st.session_state["_mycelia_tables"] = tables
            table_options = tables
            st.success(f"SQL-Dump importiert. Gefundene Tabellen: {', '.join(tables) if tables else '—'}")
        if not table_options:
            st.info("Bitte zuerst einen SQL-Dump laden, um Tabellen auszuwählen.")
        else:
            selected_tables = st.multiselect(
                "Tabellen für den Import auswählen",
                options=table_options,
                default=table_options[:1],
            )
            where_filter = st.text_input(
                "WHERE-Filter (optional, Format 'spalte=wert')",
                help="Der Filter wird sicher parametriert und auf jede ausgewählte Tabelle angewendet.",
            )
            limit = st.number_input(
                "Maximale Datensätze pro Tabelle",
                min_value=1,
                max_value=5000,
                value=100,
            )
            stability = st.slider(
                "Initiale Stabilität",
                min_value=0.1,
                max_value=1.0,
                value=0.9,
                step=0.01,
            )
            mood_payload = st.text_input(
                "Mood-Vektor (optional, drei Komma-getrennte Werte)",
                value="",
            )
            mood_vector = _parse_mood_vector(mood_payload)
            custom_chaos = st.checkbox("Eigenen Chaos-Key verwenden")
            chaos_key = None
            if custom_chaos:
                chaos_key = st.number_input(
                    "Chaos-Key",
                    min_value=0.0,
                    max_value=1.0,
                    value=0.5,
                    step=0.01,
                )
            if st.button("Import starten", type="secondary"):
                if not selected_tables:
                    st.warning("Bitte mindestens eine Tabelle auswählen.")
                else:
                    try:
                        imported = _import_tables(
                            database,
                            selected_tables,
                            where_filter or None,
                            int(limit),
                            float(stability),
                            mood_vector,
                            chaos_key,
                        )
                    except Exception as exc:  # pragma: no cover - defensive UI guard
                        st.error(f"Import fehlgeschlagen: {exc}")
                    else:
                        st.success(f"{len(imported)} Datensätze in das Myzel-Gedächtnis übernommen.")
                        if imported:
                            st.dataframe(imported, use_container_width=True)

    with tab_query:
        st.subheader("Gedächtnis-Abfragen")
        table_options = database.list_external_tables()
        selected_tables = st.multiselect(
            "Tabellen filtern",
            options=table_options,
            help="Leer lassen, um alle Tabellen zu durchsuchen.",
        )
        with st.expander("Deterministische Feldabfrage", expanded=True):
            filter_expression = st.text_input(
                "Filter (Kommagetrennt, Format spalte=wert)",
                key="filter_expression",
            )
            filters, errors = _parse_filter_expression(filter_expression)
            for error in errors:
                st.warning(error)
            limit = st.number_input(
                "Ergebnislimit",
                min_value=1,
                max_value=500,
                value=50,
                key="deterministic_limit",
            )
            if st.button("Feldabfrage ausführen"):
                combined: List[Mapping[str, object]] = []
                targets = selected_tables or [None]
                for table in targets:
                    combined.extend(
                        database.query_sql_like(
                            table=table,
                            filters=filters or None,
                            limit=int(limit),
                        )
                    )
                combined = _filter_by_tables(combined, selected_tables)
                _display_results("Deterministische Treffer", combined)
        with st.expander("Assoziative Prompt-Abfrage", expanded=True):
            cue = st.text_input("Prompt", key="assoc_prompt")
            intensity = st.slider(
                "Intensität",
                min_value=0.1,
                max_value=3.0,
                value=1.0,
                key="assoc_intensity",
            )
            limit = st.number_input(
                "Maximale Ergebnisse",
                min_value=1,
                max_value=200,
                value=20,
                key="assoc_limit",
            )
            if st.button("Assoziative Suche starten"):
                if not cue.strip():
                    st.warning("Bitte einen Prompt angeben.")
                else:
                    results = database.associative_sql_lookup(
                        cue,
                        intensity=float(intensity),
                        limit=int(limit),
                    )
                    results = _filter_by_tables(results, selected_tables)
                    _display_results("Assoziative Treffer", results)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    main()

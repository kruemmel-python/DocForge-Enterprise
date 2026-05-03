"""Direct SQL dump reader for Mycelia.

This module intentionally does *not* open SQLite, MySQL or any other database
connection.  It consumes dump text and exposes CREATE/INSERT payloads as Python
rows so that CognitiveCore can turn every row into a DAD nutrient attractor.

Supported subset:
- MySQL/MariaDB style CREATE TABLE statements, including backtick identifiers.
- INSERT INTO table [(columns...)] VALUES (...), (...);
- Basic SQL literals: quoted strings with escapes, NULL, integers, floats,
  booleans and hex blobs as strings.
- Multi-line statements.

Anything outside this ingest subset is ignored because Mycelia only needs the
row payloads, not DDL side effects.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

LOGGER = logging.getLogger(__name__)

_LAST_DUMP: "DumpImage | None" = None


@dataclass(slots=True)
class DumpTable:
    name: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class DumpImage:
    path: Path
    tables: dict[str, DumpTable] = field(default_factory=dict)


_IDENTIFIER = r"(?:`(?P<bt>[^`]+)`|\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|(?P<raw>[A-Za-z_][A-Za-z0-9_$]*))"


def _identifier_value(match: re.Match[str]) -> str:
    return next(v for v in match.groupdict().values() if v is not None)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*![\s\S]*?\*/", " ", sql)
    sql = re.sub(r"/\*[\s\S]*?\*/", " ", sql)
    sql = re.sub(r"^\s*--.*$", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"^\s*#.*$", "", sql, flags=re.MULTILINE)
    return sql


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escape = False
    for ch in sql:
        buf.append(ch)
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement[:-1].strip())
            buf.clear()
    rest = "".join(buf).strip()
    if rest:
        statements.append(rest)
    return statements


def _split_csv(text: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escape = False
    depth = 0
    for ch in text:
        if quote:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf.clear()
        else:
            buf.append(ch)
    if buf or text.strip():
        out.append("".join(buf).strip())
    return out


def _parse_literal(token: str) -> object:
    token = token.strip()
    if not token:
        return ""
    lowered = token.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if token.startswith(("'", '"')) and token.endswith(token[0]):
        body = token[1:-1]
        # Common MySQL dump escapes.  We avoid unicode_escape because it is too
        # broad and can corrupt already decoded UTF-8 text.
        replacements = {
            r"\\": "\\",
            r"\'": "'",
            r'\"': '"',
            r"\n": "\n",
            r"\r": "\r",
            r"\t": "\t",
            r"\0": "\0",
        }
        for src, dst in replacements.items():
            body = body.replace(src, dst)
        return body
    if re.fullmatch(r"0x[0-9A-Fa-f]+", token):
        return token
    try:
        if re.fullmatch(r"[-+]?\d+", token):
            return int(token)
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", token) or re.fullmatch(r"[-+]?\d+[eE][-+]?\d+", token):
            return float(token)
    except ValueError:
        pass
    return token


def _extract_create(statement: str, image: DumpImage) -> None:
    m = re.match(rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_IDENTIFIER}", statement, re.I)
    if not m:
        return
    table_name = _identifier_value(m)
    body_start = statement.find("(", m.end())
    body_end = statement.rfind(")")
    columns: list[str] = []
    if body_start != -1 and body_end != -1 and body_end > body_start:
        for part in _split_csv(statement[body_start + 1 : body_end]):
            part = part.strip()
            if not part:
                continue
            upper = part.upper()
            if upper.startswith(("PRIMARY ", "KEY ", "UNIQUE ", "INDEX ", "CONSTRAINT ", "FOREIGN ", "FULLTEXT ", "SPATIAL ")):
                continue
            cm = re.match(_IDENTIFIER, part)
            if cm:
                columns.append(_identifier_value(cm))
    image.tables.setdefault(table_name.lower(), DumpTable(table_name)).columns = columns


def _row_groups(values_text: str) -> Iterable[str]:
    quote: str | None = None
    escape = False
    depth = 0
    start: int | None = None
    for i, ch in enumerate(values_text):
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                yield values_text[start:i]
                start = None


def _extract_insert(statement: str, image: DumpImage) -> None:
    m = re.match(rf"INSERT\s+(?:IGNORE\s+)?INTO\s+{_IDENTIFIER}\s*", statement, re.I | re.S)
    if not m:
        return
    table_name = _identifier_value(m)
    rest = statement[m.end():].strip()
    columns: list[str] | None = None
    if rest.startswith("("):
        # Column list ends at its matching right paren; no nested parens here.
        end = rest.find(")")
        if end > 0:
            columns = []
            for raw in _split_csv(rest[1:end]):
                cm = re.match(_IDENTIFIER, raw.strip())
                if cm:
                    columns.append(_identifier_value(cm))
            rest = rest[end + 1 :].strip()
    vm = re.search(r"\bVALUES\b", rest, re.I)
    if not vm:
        return
    values_text = rest[vm.end():]
    table = image.tables.setdefault(table_name.lower(), DumpTable(table_name))
    for row_text in _row_groups(values_text):
        values = [_parse_literal(v) for v in _split_csv(row_text)]
        if columns is None:
            if table.columns and len(table.columns) == len(values):
                row_columns = table.columns
            else:
                row_columns = [f"col_{i}" for i in range(len(values))]
        else:
            row_columns = columns
            if not table.columns:
                table.columns = list(columns)
        row = {col: values[i] if i < len(values) else None for i, col in enumerate(row_columns)}
        table.rows.append(row)


def import_sql_file(path: str, target_engine_url: str | None = None) -> None:
    """Read *path* into an in-memory DumpImage.

    ``target_engine_url`` is accepted for backward compatibility and ignored on
    purpose: this importer never materializes a relational side database.
    """

    del target_engine_url
    sql_path = Path(path).expanduser().resolve()
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL-Datei nicht gefunden: {sql_path}")

    script = _strip_comments(sql_path.read_text(encoding="utf-8", errors="replace"))
    image = DumpImage(path=sql_path)
    for statement in _split_statements(script):
        head = statement.lstrip().upper()
        if head.startswith("CREATE TABLE"):
            _extract_create(statement, image)
        elif head.startswith("INSERT"):
            _extract_insert(statement, image)

    global _LAST_DUMP
    _LAST_DUMP = image
    LOGGER.info(
        "SQL-Dump direkt gelesen: %s Tabellen, %s Datensätze",
        len(image.tables),
        sum(len(t.rows) for t in image.tables.values()),
    )


def _require_dump() -> DumpImage:
    if _LAST_DUMP is None:
        raise RuntimeError("Es wurde noch kein SQL-Dump importiert.")
    return _LAST_DUMP


def fetch_rows(
    table: str,
    where: str | None = None,
    limit: int | None = None,
    *,
    database_path: Path | None = None,
) -> Iterable[Dict[str, object]]:
    del database_path
    image = _require_dump()
    dump_table = image.tables.get(table.lower())
    if dump_table is None:
        return []
    rows: Iterable[dict[str, object]] = dump_table.rows
    if where:
        m = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_$]*)\s*=\s*(.+?)\s*", where)
        if not m:
            raise ValueError("Nur einfache WHERE-Filter der Form column=value werden unterstützt")
        key, raw = m.group(1), m.group(2)
        expected = _parse_literal(raw)
        rows = (row for row in rows if row.get(key) == expected)
    if limit is not None:
        rows = list(rows)[: max(0, int(limit))]
    return [dict(row) for row in rows]


def list_tables(database_path: Path | None = None) -> list[str]:
    del database_path
    image = _require_dump()
    return [table.name for table in image.tables.values() if table.rows or table.columns]


def active_database_path() -> Optional[Path]:
    image = _LAST_DUMP
    return None if image is None else image.path


__all__ = ["import_sql_file", "fetch_rows", "list_tables", "active_database_path"]

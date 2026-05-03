"""SMQL v1.22 parser and compatibility translator."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .exceptions import SMQLError


_VECTOR_RE = re.compile(
    r"^\s*FIND(?:\s+(?P<table>[A-Za-z0-9_.*-]+))?\s+"
    r"ASSOCIATED\s+WITH\s+"
    r"(?P<kind>EMBEDDING|VECTOR)\s*"
    r"\[(?P<vector>.*?)\]\s*"
    r"(?:LIMIT\s+(?P<limit>\d+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)

_TEXT_RE = re.compile(
    r"^\s*FIND(?:\s+(?P<table>[A-Za-z0-9_.*-]+))?\s+"
    r"ASSOCIATED\s+WITH\s+TEXT\s+"
    r"(?P<text>.*?)\s*"
    r"(?:LIMIT\s+(?P<limit>\d+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)

_WHERE_RE = re.compile(
    r"^\s*FIND\s+(?P<table>[A-Za-z0-9_.*-]+)"
    r"(?:\s+WHERE\s+(?P<where>.*?))?"
    r"(?:\s+ASSOCIATED\s+WITH\s+(?P<cue>.*?))?"
    r"(?:\s+LIMIT\s+(?P<limit>\d+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(slots=True, frozen=True)
class SMQLQuery:
    embedding: list[float] = field(default_factory=list)
    text: str | None = None
    table: str = "mycelia_embeddings"
    limit: int = 10
    filters: dict[str, Any] = field(default_factory=dict)
    raw: str = ""

    def to_v122(self) -> str:
        if self.text is not None:
            return f"FIND ASSOCIATED WITH TEXT {_quote_text(self.text)} LIMIT {self.limit}"
        numbers = ", ".join(format(float(v), ".9g") for v in self.embedding)
        return f"FIND ASSOCIATED WITH EMBEDDING [{numbers}] LIMIT {self.limit}"

    def to_mycelia_compat(self) -> str:
        if self.text is not None:
            return f"FIND {self.table} ASSOCIATED WITH TEXT {_quote_text(self.text)} LIMIT {self.limit}"
        numbers = ", ".join(format(float(v), ".9g") for v in self.embedding)
        return f"FIND {self.table} ASSOCIATED WITH VECTOR [{numbers}] LIMIT {self.limit}"


def _quote_text(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _parse_text_literal(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise SMQLError("SMQL TEXT cue is empty")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        quote = text[0]
        body = text[1:-1]
        return (
            body.replace(r"\\", "\\")
            .replace(r"\'", "'")
            .replace(r"\"", '"')
            if quote == "'"
            else body.replace(r"\\", "\\").replace(r"\"", '"').replace(r"\'", "'")
        )
    return text


def _parse_vector(raw: str) -> list[float]:
    parts = [p.strip() for p in raw.replace("\n", " ").split(",")]
    if len(parts) == 1 and " " in parts[0]:
        parts = [p.strip() for p in parts[0].split(" ")]
    out: list[float] = []
    for part in parts:
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError as exc:
            raise SMQLError(f"Invalid vector component: {part!r}") from exc
    if not out:
        raise SMQLError("SMQL embedding vector is empty")
    return out


def _table_or_default(table: str | None, default_table: str) -> str:
    if not table or table == "*":
        return default_table
    # In table-less queries like ``FIND ASSOCIATED WITH TEXT ...`` the optional
    # table group can briefly capture ASSOCIATED during regex backtracking.
    if table.upper() == "ASSOCIATED":
        return default_table
    return table


def _limit(raw: str | None) -> int:
    return max(1, min(1000, int(raw or 10)))


def _parse_cue(cue: str) -> tuple[list[float], str | None]:
    clean = cue.strip()
    vector_match = re.match(
        r"^(?:EMBEDDING|VECTOR)\s*\[(.*?)\]$",
        clean,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if vector_match:
        return _parse_vector(vector_match.group(1)), None

    text_match = re.match(
        r"^TEXT\s+(.*?)$",
        clean,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if text_match:
        return [], _parse_text_literal(text_match.group(1))

    raise SMQLError("Only EMBEDDING [...], VECTOR [...] or TEXT '...' cues are supported by this adapter")


def parse_smql(query: str, *, default_table: str = "mycelia_embeddings") -> SMQLQuery:
    text = " ".join(str(query or "").strip().split())
    if not text:
        raise SMQLError("SMQL query is empty")

    vm = _VECTOR_RE.match(text)
    if vm:
        return SMQLQuery(
            embedding=_parse_vector(vm.group("vector") or ""),
            text=None,
            table=_table_or_default(vm.group("table"), default_table),
            limit=_limit(vm.group("limit")),
            raw=text,
        )

    tm = _TEXT_RE.match(text)
    if tm:
        cue_text = tm.group("text") or ""
        # If the optional table consumed ASSOCIATED, recover from the table-less
        # syntax by stripping the leading WITH TEXT that remains in the text group.
        if (tm.group("table") or "").upper() == "ASSOCIATED":
            cue_text = re.sub(r"^WITH\s+TEXT\s+", "", cue_text, flags=re.IGNORECASE)
        return SMQLQuery(
            embedding=[],
            text=_parse_text_literal(cue_text),
            table=_table_or_default(tm.group("table"), default_table),
            limit=_limit(tm.group("limit")),
            raw=text,
        )

    wm = _WHERE_RE.match(text)
    if wm:
        cue = (wm.group("cue") or "").strip()
        embedding, cue_text = _parse_cue(cue)
        return SMQLQuery(
            embedding=embedding,
            text=cue_text,
            table=_table_or_default(wm.group("table"), default_table),
            limit=_limit(wm.group("limit")),
            raw=text,
        )

    raise SMQLError(
        "Expected syntax: FIND ASSOCIATED WITH EMBEDDING [0.1, ...] LIMIT 3 "
        "or FIND ASSOCIATED WITH TEXT 'frage' LIMIT 3"
    )

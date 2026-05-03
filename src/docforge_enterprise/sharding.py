from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Iterable

from .hashing import stable_id, sha256_text
from .models import CodeShard, ProjectFile


_SYMBOL_RE = re.compile(
    r"^\s*(?:class|def|async\s+def|function|interface|type|export\s+function|public\s+class|private\s+class|struct|enum|trait|impl|func)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

# Language families that can be segmented reasonably well by declaration + brace matching.
_BRACE_LANGUAGE_PREFIXES = (
    "java",
    "csharp",
    "cpp",
    "c",
    "go",
    "rust",
    "php",
    "javascript",
    "typescript",
    "javascript-react",
    "typescript-react",
)

_DECLARATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "java": re.compile(
        r"(?m)^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:public|private|protected|static|final|abstract|synchronized|native|\s)+\s*"
        r"(?:class|interface|enum|record|void|[\w<>\[\], ?]+)\s+([A-Za-z_][\w]*)\s*(?:\([^;{}]*\)|extends\b|implements\b|throws\b|[<{])"
    ),
    "csharp": re.compile(
        r"(?m)^\s*(?:\[[^\]]+\]\s*)*(?:public|private|protected|internal|static|sealed|abstract|async|partial|override|virtual|extern|\s)+\s*"
        r"(?:class|interface|struct|enum|record|void|[\w<>\[\], ?]+)\s+([A-Za-z_][\w]*)\s*(?:\([^;{}]*\)|:\s*[\w<>, .]+|[<{])"
    ),
    "cpp": re.compile(
        r"(?m)^\s*(?:template\s*<[^>]+>\s*)?(?:class|struct|enum|namespace|[\w:<>~*&\s]+)\s+([A-Za-z_~][\w:~]*)\s*(?:\([^;{}]*\)|[:{])"
    ),
    "c": re.compile(
        r"(?m)^\s*(?:static|extern|inline|\s)*[\w\*\s]+\s+([A-Za-z_][\w]*)\s*\([^;{}]*\)\s*\{"
    ),
    "go": re.compile(
        r"(?m)^\s*(?:func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\([^)]*\)|type\s+([A-Za-z_][\w]*)\s+(?:struct|interface))"
    ),
    "rust": re.compile(
        r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][\w]*)?"
    ),
    "php": re.compile(
        r"(?m)^\s*(?:final\s+|abstract\s+)?(?:class|interface|trait|enum|function)\s+([A-Za-z_][\w]*)"
    ),
    "javascript": re.compile(
        r"(?m)^\s*(?:export\s+default\s+|export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)|"
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"
    ),
    "typescript": re.compile(
        r"(?m)^\s*(?:export\s+default\s+|export\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)|"
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:\w<>, \[\]|&?]*=\s*(?:async\s*)?\([^)]*\)\s*=>"
    ),
}


@dataclass(slots=True)
class ShardPlan:
    max_chars: int = 6000
    overlap: int = 600


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    total = 0
    for line in text.splitlines(keepends=True):
        total += len(line)
        offsets.append(total)
    return offsets


def _line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, max(pos, 0)) + 1


def _next_section_start(text: str, pos: int) -> int:
    match = re.search(r"(?m)^#{1,6}\s+.+$", text[pos:])
    return len(text) if match is None else pos + match.start()


def _find_matching_brace(text: str, open_pos: int) -> int:
    """Return the exclusive end offset for the brace block starting at open_pos."""
    depth = 0
    i = open_pos
    quote: str | None = None
    escaped = False
    in_line_comment = False
    in_block_comment = False

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth <= 0:
                return i + 1
        i += 1

    return len(text)


def _python_symbol_spans(content: str) -> list[tuple[int, int, tuple[str, ...]]]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    offsets = _line_offsets(content)
    spans: list[tuple[int, int, tuple[str, ...]]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
                continue
            start = offsets[max(node.lineno - 1, 0)]
            end = offsets[min(node.end_lineno, len(offsets) - 1)]
            spans.append((start, end, (node.name,)))

    spans.sort(key=lambda item: (item[0], item[1]))
    return _dedupe_nested_spans(spans)


def _language_key(language: str) -> str:
    if language in {"typescript-react", "typescript"}:
        return "typescript"
    if language in {"javascript-react", "javascript"}:
        return "javascript"
    if language.startswith("cpp"):
        return "cpp"
    if language.startswith("csharp"):
        return "csharp"
    if language == "c":
        return "c"
    return language


def _brace_symbol_spans(content: str, language: str) -> list[tuple[int, int, tuple[str, ...]]]:
    key = _language_key(language)
    pattern = _DECLARATION_PATTERNS.get(key)
    if pattern is None:
        return []

    spans: list[tuple[int, int, tuple[str, ...]]] = []
    for match in pattern.finditer(content):
        symbol = next((g for g in match.groups() if g), None) or "<anonymous>"
        start = _line_start(content, match.start())
        brace_pos = content.find("{", match.end() - 1)
        semicolon_pos = content.find(";", match.end() - 1)
        if brace_pos == -1 or (semicolon_pos != -1 and semicolon_pos < brace_pos):
            # TypeScript type aliases/interfaces and declarations without bodies:
            end = _next_declaration_or_reasonable_boundary(content, match.end(), pattern)
        else:
            end = _find_matching_brace(content, brace_pos)
        if end > start:
            spans.append((start, end, (symbol,)))

    spans.sort(key=lambda item: (item[0], item[1]))
    return _dedupe_nested_spans(spans)


def _markdown_section_spans(content: str) -> list[tuple[int, int, tuple[str, ...]]]:
    headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", content))
    if not headings:
        return []
    spans: list[tuple[int, int, tuple[str, ...]]] = []
    for idx, heading in enumerate(headings):
        start = heading.start()
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(content)
        title = heading.group(1).strip()
        spans.append((start, end, (title,)))
    return spans


def _sql_spans(content: str) -> list[tuple[int, int, tuple[str, ...]]]:
    statement_re = re.compile(
        r"(?ims)^\s*(create\s+(?:or\s+replace\s+)?(?:table|view|function|procedure|index|trigger)\s+[\w.\"`]+|"
        r"alter\s+table\s+[\w.\"`]+|insert\s+into\s+[\w.\"`]+|select\b).*?(?:;\s*$|$)"
    )
    spans: list[tuple[int, int, tuple[str, ...]]] = []
    for match in statement_re.finditer(content):
        head = re.sub(r"\s+", " ", match.group(1)).strip()
        spans.append((match.start(), match.end(), (head[:120],)))
    return spans


def _next_declaration_or_reasonable_boundary(content: str, pos: int, pattern: re.Pattern[str]) -> int:
    next_match = pattern.search(content, pos)
    if next_match:
        return _line_start(content, next_match.start())
    newline = content.find("\n\n", pos)
    return len(content) if newline == -1 else newline


def _dedupe_nested_spans(spans: list[tuple[int, int, tuple[str, ...]]]) -> list[tuple[int, int, tuple[str, ...]]]:
    """Keep meaningful nested symbols but avoid exact duplicate spans."""
    seen: set[tuple[int, int, tuple[str, ...]]] = set()
    result: list[tuple[int, int, tuple[str, ...]]] = []
    for span in spans:
        key = (span[0], span[1], span[2])
        if key not in seen and span[1] > span[0]:
            seen.add(key)
            result.append(span)
    return result


def _symbol_spans(project_file: ProjectFile) -> list[tuple[int, int, tuple[str, ...]]]:
    language = project_file.language
    content = project_file.content

    if language == "python":
        return _python_symbol_spans(content)

    if language == "markdown":
        return _markdown_section_spans(content)

    if language == "sql":
        return _sql_spans(content)

    if _language_key(language) in _DECLARATION_PATTERNS:
        return _brace_symbol_spans(content, language)

    return []


def _split_large_span(
    project_file: ProjectFile,
    start: int,
    end: int,
    ordinal_start: int,
    plan: ShardPlan,
    symbols: tuple[str, ...] = (),
) -> list[CodeShard]:
    content = project_file.content
    shards: list[CodeShard] = []
    pos = start
    ordinal = ordinal_start
    while pos < end:
        chunk_end = min(pos + plan.max_chars, end)
        if chunk_end < end:
            newline = content.rfind("\n", pos, chunk_end)
            if newline > pos + plan.max_chars // 2:
                chunk_end = newline
        text = content[pos:chunk_end]
        shards.append(
            CodeShard(
                id=stable_id(project_file.relative_path, project_file.sha256, pos, chunk_end),
                file_path=project_file.relative_path,
                language=project_file.language,
                kind=project_file.kind,
                content=text,
                char_start=pos,
                char_end=chunk_end,
                sha256=sha256_text(text),
                ordinal=ordinal,
                symbols=symbols,
            )
        )
        ordinal += 1
        pos = chunk_end if chunk_end >= end else max(chunk_end - plan.overlap, pos + 1)
    return shards


def shard_file(project_file: ProjectFile, plan: ShardPlan) -> list[CodeShard]:
    content = project_file.content
    if not content:
        return []

    spans = _symbol_spans(project_file)
    if spans:
        shards: list[CodeShard] = []
        ordinal = 0
        covered_until = 0

        for start, end, symbols in spans:
            if start > covered_until:
                shards.extend(_split_large_span(project_file, covered_until, start, ordinal, plan))
                ordinal = len(shards)

            # If declarations overlap because of nested functions/classes, keep the outer coverage
            # monotonic but still emit a symbol shard. This gives better retrieval anchors.
            shard_start = max(0, start)
            shard_end = max(shard_start, end)
            shards.extend(_split_large_span(project_file, shard_start, shard_end, ordinal, plan, symbols))
            ordinal = len(shards)
            covered_until = max(covered_until, shard_end)

        if covered_until < len(content):
            shards.extend(_split_large_span(project_file, covered_until, len(content), ordinal, plan))
        return [s for s in shards if s.content.strip()]

    symbols = tuple(dict.fromkeys(_SYMBOL_RE.findall(content)))
    return _split_large_span(project_file, 0, len(content), 0, plan, symbols)


def shard_project(files: Iterable[ProjectFile], plan: ShardPlan) -> list[CodeShard]:
    shards: list[CodeShard] = []
    for project_file in files:
        shards.extend(shard_file(project_file, plan))
    return shards

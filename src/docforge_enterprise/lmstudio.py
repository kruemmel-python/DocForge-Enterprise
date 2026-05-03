from __future__ import annotations

import ast
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import LMStudioSettings
from .resilience import RequestBudget, is_timeout_exception, retry_with_budget


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class JsonExtractionResult:
    payload: dict[str, Any]
    repaired: bool = False
    strategy: str = "strict"


@dataclass(slots=True)
class LMStudioChatClient:
    settings: LMStudioSettings
    json_repairs: int = field(default=0, init=False)
    timeouts: int = field(default=0, init=False)
    retries_used: int = field(default=0, init=False)

    def _base_url(self) -> str:
        raw = self.settings.base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(raw)
        path = parsed.path.rstrip("/")
        if path in {"", "/"}:
            return urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, "/v1", parsed.query, parsed.fragment)
            ).rstrip("/")
        return raw

    def _post(self, path: str, payload: Mapping[str, Any], *, timeout: float, label: str) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._base_url() + "/" + path.lstrip("/"),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        attempts_before = self.retries_used

        def once() -> dict[str, Any]:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.URLError as exc:
                if is_timeout_exception(exc):
                    raise
                raise LLMError(f"LM Studio request failed: {exc}") from exc
            except TimeoutError:
                raise
            except json.JSONDecodeError as exc:
                raise LLMError("LM Studio returned invalid JSON") from exc

        try:
            return retry_with_budget(
                once,
                RequestBudget(
                    label=label,
                    timeout_seconds=timeout,
                    retries=self.settings.request_retries,
                    backoff_seconds=self.settings.retry_backoff_seconds,
                ),
            )
        except Exception as exc:
            if is_timeout_exception(exc):
                self.timeouts += 1
                raise LLMError(str(exc)) from exc
            raise
        finally:
            # retry_with_budget has no callback, so estimate retry pressure from timeout count elsewhere.
            self.retries_used = attempts_before

    def chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        label: str = "lmstudio.chat",
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.settings.chat_model,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        response = self._post(
            "/chat/completions",
            payload,
            timeout=self.settings.chat_timeout_seconds if timeout is None else timeout,
            label=label,
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"LM Studio response has no choices: {response!r}")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise LLMError("LM Studio choice is malformed")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise LLMError("LM Studio message is malformed")
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMError("LM Studio message content is missing")
        return content

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        repair_attempts: int | None = None,
        timeout: float | None = None,
        label: str = "lmstudio.chat_json",
    ) -> dict[str, Any]:
        raw = self.chat(system=system, user=user, max_tokens=max_tokens, timeout=timeout, label=label)
        attempts = self.settings.json_repair_attempts if repair_attempts is None else repair_attempts

        try:
            result = extract_json_result(raw)
            if result.repaired:
                self.json_repairs += 1
            return result.payload
        except LLMError as first_exc:
            if attempts <= 0:
                raise

            repair_prompt = f"""
The previous model response was intended to be a single JSON object but is invalid.
Return only a valid JSON object. Do not add markdown, explanations or code fences.

Invalid response:
{raw[:12000]}

Original task:
{user[:6000]}
""".strip()

            repaired_raw = self.chat(
                system="You repair invalid JSON into one strict JSON object.",
                user=repair_prompt,
                temperature=0.0,
                max_tokens=max_tokens,
                timeout=timeout,
                label=f"{label}.repair",
            )
            try:
                result = extract_json_result(repaired_raw)
            except LLMError as repair_exc:
                raise LLMError(f"JSON extraction failed: {first_exc}; repair failed: {repair_exc}") from repair_exc

            self.json_repairs += 1
            return result.payload


def extract_json(raw: str) -> dict[str, Any]:
    return extract_json_result(raw).payload


def extract_json_result(raw: str) -> JsonExtractionResult:
    text = _strip_markdown(raw.strip())

    parsed = _try_json(text)
    if isinstance(parsed, dict):
        return JsonExtractionResult(parsed, repaired=False, strategy="strict")
    if isinstance(parsed, list):
        first = next((item for item in parsed if isinstance(item, dict)), None)
        if first is not None:
            return JsonExtractionResult(first, repaired=True, strategy="list-first-object")

    candidates = _balanced_json_candidates(text)
    last_error: Exception | None = None

    for candidate in candidates:
        for strategy, repaired in _repair_candidates(candidate):
            parsed = _try_json(repaired)
            if isinstance(parsed, dict):
                return JsonExtractionResult(parsed, repaired=(strategy != "candidate"), strategy=strategy)
            if isinstance(parsed, list):
                first = next((item for item in parsed if isinstance(item, dict)), None)
                if first is not None:
                    return JsonExtractionResult(first, repaired=True, strategy=f"{strategy}:list-first-object")
            try:
                literal = ast.literal_eval(repaired)
                if isinstance(literal, dict):
                    return JsonExtractionResult(_stringify_keys(literal), repaired=True, strategy=f"{strategy}:literal-eval")
            except Exception as exc:  # noqa: BLE001 - collected for diagnostics only
                last_error = exc

    if last_error is not None:
        raise LLMError(f"No valid JSON object found after repair attempts: {last_error}")
    raise LLMError(f"No JSON object found in model response: {raw[:500]}")


def _strip_markdown(text: str) -> str:
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _balanced_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    stack: list[str] = []
    start: int | None = None
    quote: str | None = None
    escaped = False

    pairs = {"{": "}", "[": "]"}
    closing = set(pairs.values())

    for idx, ch in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue

        if ch in {'"', "'"}:
            quote = ch
            continue

        if ch in pairs:
            if not stack:
                start = idx
            stack.append(pairs[ch])
            continue

        if ch in closing and stack:
            expected = stack.pop()
            if ch != expected:
                stack.clear()
                start = None
                continue
            if not stack and start is not None:
                candidates.append(text[start : idx + 1])
                start = None

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidate = text[first : last + 1]
        if candidate not in candidates:
            candidates.append(candidate)

    return candidates


def _repair_candidates(candidate: str) -> list[tuple[str, str]]:
    normalized = candidate.strip().replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    no_trailing_commas = re.sub(r",\s*([}\]])", r"\1", normalized)
    quoted_keys = re.sub(r"(?m)([{,]\s*)([A-Za-z_][A-Za-z0-9_\-]*)(\s*:)", r'\1"\2"\3', no_trailing_commas)
    pythonish = no_trailing_commas
    return [
        ("candidate", normalized),
        ("trailing-comma", no_trailing_commas),
        ("quoted-keys", quoted_keys),
        ("python-literal", pythonish),
    ]


def _stringify_keys(value: dict[Any, Any]) -> dict[str, Any]:
    def convert(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(k): convert(v) for k, v in item.items()}
        if isinstance(item, list):
            return [convert(v) for v in item]
        return item

    return {str(k): convert(v) for k, v in value.items()}

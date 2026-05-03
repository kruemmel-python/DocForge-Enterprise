from __future__ import annotations

import random
import socket
import time
import urllib.error
from dataclasses import dataclass
from typing import Callable, TypeVar


T = TypeVar("T")


class RequestTimeout(RuntimeError):
    """Raised when a budgeted local model or gateway request timed out."""


@dataclass(frozen=True, slots=True)
class RequestBudget:
    label: str
    timeout_seconds: float
    retries: int = 3
    backoff_seconds: float = 2.0


def is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout, RequestTimeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError) and "timed out" in str(exc.reason).lower():
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def retry_with_budget(fn: Callable[[], T], budget: RequestBudget) -> T:
    last_error: BaseException | None = None
    attempts = max(0, budget.retries) + 1

    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - caller chooses which errors are retryable
            if not is_timeout_exception(exc) and attempt == 0:
                raise
            if not is_timeout_exception(exc):
                raise
            last_error = exc

        if attempt < attempts - 1:
            sleep_s = max(0.0, budget.backoff_seconds) * (2 ** attempt)
            sleep_s += random.uniform(0.0, 0.4)
            time.sleep(sleep_s)

    raise RequestTimeout(
        f"{budget.label} timed out after {attempts} attempt(s): {last_error}"
    ) from last_error

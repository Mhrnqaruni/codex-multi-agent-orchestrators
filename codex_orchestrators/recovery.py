"""Bounded recovery decisions; no host reachability probes."""

from dataclasses import dataclass, field
from enum import Enum
import time


class Failure(str, Enum):
    SUCCESS = "success"
    TRANSIENT = "transient"
    TERMINAL = "terminal"


def classify_failure(stderr: str, returncode: int) -> Failure:
    lowered = stderr.lower()
    permanent = (
        "insufficient_quota",
        "quota exceeded",
        "billing",
        "unauthorized",
        "authentication",
        "invalid api key",
        "permission denied",
        "policy denied",
        "unsupported",
        "invalid request",
        "budget exhausted",
        "output limit",
        "candidate changed",
        "malformed event",
    )
    if any(marker in lowered for marker in permanent):
        return Failure.TERMINAL
    if returncode == 0:
        return Failure.SUCCESS
    transient = (
        "rate limit",
        "rate_limit",
        "429",
        "connection reset",
        "connection refused",
        "network",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "503",
    )
    if any(marker in lowered for marker in transient):
        return Failure.TRANSIENT
    return Failure.TERMINAL


@dataclass
class RunBudget:
    """One budget per workflow instance, including continuations and retries.

    Calls/time are enforceable ceilings, not an estimate or cap on dollar cost.
    Configure provider/account spend limits separately.
    """

    max_calls: int = 24
    max_seconds: float = 7200
    calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not 1 <= self.max_calls <= 100 or not 1 <= self.max_seconds <= 86400:
            raise ValueError("Budget outside supported limits")

    def reserve(self) -> float:
        remaining = self.max_seconds - (time.monotonic() - self.started)
        if self.calls >= self.max_calls or remaining <= 0:
            raise RuntimeError("budget exhausted")
        self.calls += 1
        return remaining


def retry_delay(attempt: int) -> float:
    """Deterministic capped backoff; injectable sleep keeps tests fast."""
    return min(30.0, 2.0 ** min(max(attempt, 0), 5))

"""Data-health detection: catches a tool that's alive but silently failing every call.

Generalizes the 2026-09-19 finding that artist-agent's LaunchAgent reported
healthy for days while its vision self-critique silently failed every run
(a missing credential) -- the CPU/liveness check in core.py can't see this
class of bug at all, since the process itself runs and exits fine every
time. This module looks at what a tool actually *did* on its recent calls,
read from processing_log/fetch_log/api_call_log, instead of whether its
process is still breathing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolStats:
    """One tool's call outcomes in the lookback window, from one log table."""

    table: str
    tool_name: str
    total: int
    failures: int

    @property
    def failure_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0

    @property
    def key(self) -> str:
        return f"{self.table}:{self.tool_name}"


@dataclass
class DegradedTool:
    table: str
    tool_name: str
    total: int
    failures: int
    failure_rate: float


def check_data_health(
    previous_degraded: frozenset[str],
    stats: list[ToolStats],
    min_calls: int = 5,
    failure_rate_threshold: float = 0.5,
) -> tuple[frozenset[str], list[DegradedTool]]:
    """One detection pass. Returns (next_degraded_keys, newly degraded tools to alert on).

    Only alerts on a *transition* into degraded -- a tool that's been broken
    for three days would otherwise notify on every single poll. Recovering
    (dropping out of next_degraded) then degrading again alerts once more.

    A tool with fewer than min_calls in the window is skipped entirely: a
    single failed call out of one attempt is noise, not a signal.
    """
    next_degraded: set[str] = set()
    newly: list[DegradedTool] = []

    for s in stats:
        if s.total < min_calls:
            continue
        if s.failure_rate < failure_rate_threshold:
            continue

        next_degraded.add(s.key)
        if s.key not in previous_degraded:
            newly.append(
                DegradedTool(
                    table=s.table,
                    tool_name=s.tool_name,
                    total=s.total,
                    failures=s.failures,
                    failure_rate=s.failure_rate,
                )
            )

    return frozenset(next_degraded), newly

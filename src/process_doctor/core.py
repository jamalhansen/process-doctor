"""Pure detection logic for process-doctor.

Generalizes the 2026-09-17 artist-agent debugging finding: a launchd-spawned
job that's actually stuck shows near-zero CPU growth across polls while
`launchctl` still reports it running, regardless of which tool it is. This
module takes already-resolved data (parsed `launchctl list`/`ps` output, an
injected CPU lookup) so the decision logic is testable without shelling out --
subprocess calls live in system.py.

Known limitation, not solved here: a legitimately slow, low-CPU job (e.g.
one blocked on a slow network response for its entire duration) is
indistinguishable from a truly stuck one by this signature alone. Defaults
are set conservatively (600s) to reduce false positives; tune stuck_after_seconds
per label if a real job needs more headroom.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# 2026-09-24: discovery-loop, ollama-keepalive and voicejournal were renamed
# from com.jamalhansen.* to com.localfirst.* so every LaunchAgent lives under
# one prefix; this no longer needs to watch two.
LOCALFIRST_PREFIX = "com.localfirst."


def parse_launchctl_list(output: str) -> dict[str, int | None]:
    """Parse `launchctl list` output into {label: pid or None}, com.localfirst.* only."""
    jobs: dict[str, int | None] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid_str, _status, label = parts
        if not label.startswith(LOCALFIRST_PREFIX):
            continue
        jobs[label] = None if pid_str == "-" else int(pid_str)
    return jobs


_TIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")


def parse_ps_time(value: str) -> float:
    """Parse ps's TIME field -- [[DD-]HH:]MM:SS[.ss] -- into seconds."""
    m = _TIME_RE.match(value.strip())
    if not m:
        raise ValueError(f"unrecognized ps time format: {value!r}")
    days, hours, minutes, seconds = m.groups()
    total = float(seconds) + int(minutes) * 60
    if hours:
        total += int(hours) * 3600
    if days:
        total += int(days) * 86400
    return total


def descendant_cpu_seconds(ps_rows: list[tuple[int, int, str]], root_pid: int) -> float:
    """Sum ps TIME across root_pid and every descendant, from (pid, ppid, time) rows."""
    children: dict[int, list[int]] = {}
    times: dict[int, str] = {}
    for pid, ppid, time_str in ps_rows:
        children.setdefault(ppid, []).append(pid)
        times[pid] = time_str

    if root_pid not in times:
        return 0.0

    total = 0.0
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += parse_ps_time(times[pid])
        stack.extend(children.get(pid, []))
    return total


@dataclass
class JobState:
    pid: int
    cpu_seconds: float
    first_seen: float


@dataclass
class StuckJob:
    label: str
    pid: int
    elapsed_seconds: float
    cpu_seconds: float
    # Why it was judged stuck: "flat-cpu" (the original heuristic),
    # "stale-heartbeat" (the job heartbeats and the beat stopped), or
    # "max-runtime" (the per-job ceiling, whatever else it was doing).
    reason: str = "flat-cpu"


# Per-job settings, keyed by LaunchAgent label. Both optional:
#   stuck_after  -- replaces the global stuck_after_seconds for this job
#   max_runtime  -- hard ceiling in seconds; past it the job is stuck even if
#                   it is still heartbeating or burning CPU
JobOverrides = dict[str, dict[str, float]]


def check_jobs(
    previous_state: dict[str, JobState],
    current_jobs: dict[str, int | None],
    cpu_lookup: Callable[[int], float],
    now: float,
    stuck_after_seconds: float = 600.0,
    cpu_epsilon_seconds: float = 2.0,
    heartbeat_lookup: Callable[[str], float | None] | None = None,
    job_overrides: JobOverrides | None = None,
) -> tuple[dict[str, JobState], list[StuckJob]]:
    """One detection pass. Returns (next_state, jobs newly judged stuck).

    Two ways to be stuck, plus a ceiling:

    - A job that heartbeats (see local_first_common.heartbeat) is stuck when
      its most recent beat is older than stuck_after_seconds. A beat from a
      previous run (older than this run's first sighting) doesn't count, so a
      job that used to heartbeat and now hangs before its first beat is still
      caught by the CPU rule. Added 2026-10-03 after the CPU rule killed the
      discovery run most mornings: waiting on a 16 s LLM call per item looks
      exactly like hanging.
    - A job that doesn't heartbeat is stuck once it has run at least
      stuck_after_seconds under the same pid with less than
      cpu_epsilon_seconds of total CPU-time growth since first observed.
    - Either way, a job past its max_runtime override is stuck.

    Stuck jobs are dropped from next_state -- the caller kills them, and the
    next poll starts tracking fresh once (if) they restart.
    """
    next_state: dict[str, JobState] = {}
    stuck: list[StuckJob] = []
    overrides = job_overrides or {}

    for label, pid in current_jobs.items():
        if pid is None:
            continue  # not currently running -- nothing to track

        cpu_now = cpu_lookup(pid)
        prev = previous_state.get(label)

        if prev is None or prev.pid != pid:
            next_state[label] = JobState(pid=pid, cpu_seconds=cpu_now, first_seen=now)
            continue

        elapsed = now - prev.first_seen
        growth = cpu_now - prev.cpu_seconds
        job = overrides.get(label, {})
        stuck_after = float(job.get("stuck_after", stuck_after_seconds))
        max_runtime = job.get("max_runtime")

        # A beat counts for this run if it is no older than our first sighting
        # minus one stuck window: first_seen can trail the real start by a poll.
        beat = heartbeat_lookup(label) if heartbeat_lookup else None
        beats_this_run = beat is not None and beat >= prev.first_seen - stuck_after

        reason = None
        if max_runtime is not None and elapsed >= float(max_runtime):
            reason = "max-runtime"
        elif beats_this_run:
            if now - beat >= stuck_after:
                reason = "stale-heartbeat"
        elif elapsed >= stuck_after and growth < cpu_epsilon_seconds:
            reason = "flat-cpu"

        if reason:
            stuck.append(
                StuckJob(label=label, pid=pid, elapsed_seconds=elapsed, cpu_seconds=cpu_now, reason=reason)
            )
            continue

        next_state[label] = JobState(pid=pid, cpu_seconds=cpu_now, first_seen=prev.first_seen)

    return next_state, stuck

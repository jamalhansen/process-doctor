"""Subprocess-touching side of process-doctor: launchctl, ps, kill, osascript.

Kept separate from core.py so the detection logic stays testable without
shelling out. State and log paths are overridable via env vars so tests
never touch the real files under ~/sync/local-first/.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from local_first_common.config import get_setting
from local_first_common.heartbeat import last_heartbeat
from local_first_common.tracking import get_tracking_db_path

from .core import (
    JobOverrides,
    JobState,
    StuckJob,
    check_jobs,
    descendant_cpu_seconds,
    parse_launchctl_list,
)

TOOL_NAME = "process-doctor"
from .data_health import DegradedTool, ToolStats, check_data_health

# One query per table: processing_log has tool_name directly; fetch_log and
# api_call_log key by tool_id and need the tools table for the real name.
_STATS_QUERIES: dict[str, str] = {
    "processing_log": """
        SELECT tool_name,
               COUNT(*) AS total,
               SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) AS failures
        FROM processing_log
        WHERE created_at > ?
        GROUP BY tool_name
    """,
    "fetch_log": """
        SELECT t.name AS tool_name,
               COUNT(*) AS total,
               SUM(CASE WHEN NOT fl.success THEN 1 ELSE 0 END) AS failures
        FROM fetch_log fl
        JOIN tools t ON fl.tool_id = t.id
        WHERE fl.attempted_at > ?
        GROUP BY t.name
    """,
    "api_call_log": """
        SELECT t.name AS tool_name,
               COUNT(*) AS total,
               SUM(CASE WHEN NOT acl.success THEN 1 ELSE 0 END) AS failures
        FROM api_call_log acl
        JOIN tools t ON acl.tool_id = t.id
        WHERE acl.attempted_at > ?
        GROUP BY t.name
    """,
}


def state_path() -> Path:
    override = os.environ.get("PROCESS_DOCTOR_STATE_PATH")
    return Path(override) if override else Path.home() / "sync" / "local-first" / "process-doctor-state.json"


def log_path() -> Path:
    override = os.environ.get("PROCESS_DOCTOR_LOG_PATH")
    return Path(override) if override else Path.home() / "sync" / "local-first" / "process-doctor.log"


def data_health_state_path() -> Path:
    override = os.environ.get("PROCESS_DOCTOR_DATA_HEALTH_STATE_PATH")
    return (
        Path(override)
        if override
        else Path.home() / "sync" / "local-first" / "process-doctor-data-health-state.json"
    )


def launch_agents_dir() -> Path:
    override = os.environ.get("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR")
    return Path(override) if override else Path.home() / "Library" / "LaunchAgents"


def is_keep_alive(label: str) -> bool:
    """True if this LaunchAgent's plist has KeepAlive set.

    A KeepAlive service (llm-gateway-service, http-retriever-service) is a
    persistent daemon expected to sit idle between requests -- flat CPU is
    its normal resting state, not the launchd-stuck signature this tool
    exists to catch. Confirmed live 2026-09-19: without this check, both
    services were being killed and restarted by every ~15-minute poll, all
    day, because idle looked identical to hung.

    Missing/unreadable plist -> False, falling back to the existing
    stuck-detection behavior rather than silently exempting a job this
    can't actually read.
    """
    path = launch_agents_dir() / f"{label}.plist"
    if not path.exists():
        return False
    try:
        import plistlib

        with path.open("rb") as f:
            data = plistlib.load(f)
    except Exception:  # noqa: BLE001 - a malformed/unreadable plist should not be treated as KeepAlive
        return False

    keep_alive = data.get("KeepAlive")
    if isinstance(keep_alive, dict):
        return True  # a conditional KeepAlive dict still means "idle is normal, restart on exit"
    return bool(keep_alive)


def get_launchctl_jobs() -> dict[str, int | None]:
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=True)
    return parse_launchctl_list(result.stdout)


def get_ps_rows() -> list[tuple[int, int, str]]:
    result = subprocess.run(["ps", "-eo", "pid,ppid,time"], capture_output=True, text=True, check=True)
    rows: list[tuple[int, int, str]] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def cpu_lookup_for(root_pid: int) -> float:
    return descendant_cpu_seconds(get_ps_rows(), root_pid)


def load_state() -> dict[str, JobState]:
    path = state_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {label: JobState(**data) for label, data in raw.items()}


def save_state(state: dict[str, JobState]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {label: vars(js) for label, js in state.items()}
    path.write_text(json.dumps(raw, indent=2))


def _descendants(root_pid: int, rows: list[tuple[int, int, str]]) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid, _ in rows:
        children.setdefault(ppid, []).append(pid)
    result: list[int] = []
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        stack.extend(children.get(pid, []))
    return result


def kill_tree(root_pid: int) -> None:
    pids = _descendants(root_pid, get_ps_rows())
    for pid in pids:
        subprocess.run(["kill", "-TERM", str(pid)], capture_output=True, check=False)
    time.sleep(2)
    for pid in pids:
        subprocess.run(["kill", "-KILL", str(pid)], capture_output=True, check=False)


def _as_applescript_literal(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> None:
    script = f"display notification {_as_applescript_literal(message)} with title {_as_applescript_literal(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True, check=False)


def log_event(message: str) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a") as f:
        f.write(f"{ts} {message}\n")


def job_overrides() -> JobOverrides:
    """Per-job settings from ~/.config/local-first/process-doctor.toml:

        [jobs."com.localfirst.discovery-loop"]
        stuck_after = 1800   # seconds without a heartbeat (or with flat CPU)
        max_runtime = 7200   # hard ceiling

    Unknown keys are ignored; non-numeric values are dropped rather than crashing the poll.
    """
    raw = get_setting(TOOL_NAME, "jobs", default={}) or {}
    out: JobOverrides = {}
    for label, settings in raw.items():
        if not isinstance(settings, dict):
            continue
        clean = {}
        for key in ("stuck_after", "max_runtime"):
            try:
                if key in settings:
                    clean[key] = float(settings[key])
            except (TypeError, ValueError):
                continue
        if clean:
            out[label] = clean
    return out


def run_check(stuck_after_seconds: float = 600.0, cpu_epsilon_seconds: float = 2.0) -> list[StuckJob]:
    """One full poll: load state, detect, kill+log+notify on anything stuck, save state."""
    state = load_state()
    jobs = {
        label: pid for label, pid in get_launchctl_jobs().items() if not is_keep_alive(label)
    }
    next_state, stuck = check_jobs(
        state, jobs, cpu_lookup_for, time.time(), stuck_after_seconds, cpu_epsilon_seconds,
        heartbeat_lookup=last_heartbeat, job_overrides=job_overrides(),
    )
    save_state(next_state)

    for job in stuck:
        log_event(
            f"STUCK {job.label} pid={job.pid} elapsed={job.elapsed_seconds:.0f}s "
            f"cpu={job.cpu_seconds:.1f}s reason={job.reason} -- killing"
        )
        kill_tree(job.pid)
        notify("process-doctor", f"{job.label} was stuck (pid {job.pid}) -- killed it.")

    return stuck


def load_data_health_state() -> frozenset[str]:
    path = data_health_state_path()
    if not path.exists():
        return frozenset()
    return frozenset(json.loads(path.read_text()))


def save_data_health_state(degraded: frozenset[str]) -> None:
    path = data_health_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(degraded), indent=2))


def get_tool_stats(lookback_hours: float = 24.0) -> list[ToolStats]:
    """Query processing_log/fetch_log/api_call_log for each tool's recent success rate.

    Never raises: the tracking DB is best-effort infrastructure shared with
    every tool that writes to it (including one that might currently be
    mid-write), so a lock conflict or missing table degrades to "no stats
    this poll" rather than crashing the check.
    """
    db_path = get_tracking_db_path()
    if not db_path.exists():
        return []

    cutoff = datetime.now() - timedelta(hours=lookback_hours)  # noqa: DTZ005 - must stay naive to compare against these tables' naive CURRENT_TIMESTAMP columns
    stats: list[ToolStats] = []
    try:
        import duckdb

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            for table, query in _STATS_QUERIES.items():
                for tool_name, total, failures in conn.execute(query, [cutoff]).fetchall():
                    stats.append(
                        ToolStats(
                            table=table,
                            tool_name=tool_name,
                            total=total,
                            failures=failures or 0,
                        )
                    )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - best-effort read against a DB other tools may be writing to concurrently
        return []

    return stats


def run_data_health_check(
    lookback_hours: float = 24.0,
    min_calls: int = 5,
    failure_rate_threshold: float = 0.5,
) -> list[DegradedTool]:
    """One full data-health poll: query, detect newly-degraded tools, log+notify, save state.

    Unlike run_check(), never kills anything -- a degraded tool's process is
    running fine; the fix is a config/credential problem, not a restart.
    """
    previous = load_data_health_state()
    stats = get_tool_stats(lookback_hours=lookback_hours)
    next_degraded, newly = check_data_health(
        previous, stats, min_calls=min_calls, failure_rate_threshold=failure_rate_threshold
    )
    save_data_health_state(next_degraded)

    for tool in newly:
        log_event(
            f"DEGRADED {tool.tool_name} ({tool.table}) failure_rate="
            f"{tool.failure_rate:.0%} ({tool.failures}/{tool.total} calls, "
            f"last {lookback_hours:.0f}h)"
        )
        notify(
            "process-doctor",
            f"{tool.tool_name} is failing {tool.failure_rate:.0%} of its "
            f"{tool.table} calls ({tool.failures}/{tool.total}, last "
            f"{lookback_hours:.0f}h) -- process is running fine, check config/credentials.",
        )

    return newly

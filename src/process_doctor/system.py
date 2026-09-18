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
from pathlib import Path

from .core import (
    JobState,
    StuckJob,
    check_jobs,
    descendant_cpu_seconds,
    parse_launchctl_list,
)


def state_path() -> Path:
    override = os.environ.get("PROCESS_DOCTOR_STATE_PATH")
    return Path(override) if override else Path.home() / "sync" / "local-first" / "process-doctor-state.json"


def log_path() -> Path:
    override = os.environ.get("PROCESS_DOCTOR_LOG_PATH")
    return Path(override) if override else Path.home() / "sync" / "local-first" / "process-doctor.log"


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


def run_check(stuck_after_seconds: float = 600.0, cpu_epsilon_seconds: float = 2.0) -> list[StuckJob]:
    """One full poll: load state, detect, kill+log+notify on anything stuck, save state."""
    state = load_state()
    jobs = get_launchctl_jobs()
    next_state, stuck = check_jobs(
        state, jobs, cpu_lookup_for, time.time(), stuck_after_seconds, cpu_epsilon_seconds
    )
    save_state(next_state)

    for job in stuck:
        log_event(
            f"STUCK {job.label} pid={job.pid} elapsed={job.elapsed_seconds:.0f}s "
            f"cpu={job.cpu_seconds:.1f}s -- killing"
        )
        kill_tree(job.pid)
        notify("process-doctor", f"{job.label} was stuck (pid {job.pid}) -- killed it.")

    return stuck

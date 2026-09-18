import subprocess
from unittest.mock import patch

from process_doctor import system


def test_state_roundtrips_through_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_DOCTOR_STATE_PATH", str(tmp_path / "state.json"))
    from process_doctor.core import JobState

    state = {"com.localfirst.x": JobState(pid=1, cpu_seconds=2.0, first_seen=3.0)}
    system.save_state(state)
    assert system.load_state() == state


def test_load_state_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_DOCTOR_STATE_PATH", str(tmp_path / "nope.json"))
    assert system.load_state() == {}


def test_get_launchctl_jobs_parses_real_subprocess_shape():
    fake = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="-\t0\tcom.localfirst.artist-agent\n"
    )
    with patch("process_doctor.system.subprocess.run", return_value=fake):
        assert system.get_launchctl_jobs() == {"com.localfirst.artist-agent": None}


def test_get_ps_rows_skips_header_and_malformed_lines():
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="  PID  PPID TIME\n  100     1 0:01.00\ngarbage\n",
    )
    with patch("process_doctor.system.subprocess.run", return_value=fake):
        assert system.get_ps_rows() == [(100, 1, "0:01.00")]


def test_kill_tree_sends_term_then_kill_to_whole_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(system.time, "sleep", lambda _: None)
    rows = [(100, 1, "0:01.00"), (101, 100, "0:01.00")]
    calls = []
    with (
        patch("process_doctor.system.get_ps_rows", return_value=rows),
        patch("process_doctor.system.subprocess.run", side_effect=lambda cmd, **kw: calls.append(cmd)),
    ):
        system.kill_tree(100)
    signals_sent = {(c[1], c[2]) for c in calls}
    assert ("-TERM", "100") in signals_sent
    assert ("-TERM", "101") in signals_sent
    assert ("-KILL", "100") in signals_sent
    assert ("-KILL", "101") in signals_sent


def test_notify_escapes_quotes_for_applescript():
    with patch("process_doctor.system.subprocess.run") as mock_run:
        system.notify('a "title"', "a message")
    script = mock_run.call_args.args[0][2]
    assert '\\"title\\"' in script


def test_log_event_appends_a_timestamped_line(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_DOCTOR_LOG_PATH", str(tmp_path / "doctor.log"))
    system.log_event("hello")
    system.log_event("world")
    lines = (tmp_path / "doctor.log").read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("hello")
    assert lines[1].endswith("world")


def test_run_check_kills_and_logs_stuck_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_DOCTOR_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("PROCESS_DOCTOR_LOG_PATH", str(tmp_path / "doctor.log"))
    from process_doctor.core import JobState

    system.save_state({"com.localfirst.x": JobState(pid=42, cpu_seconds=1.9, first_seen=0.0)})

    with (
        patch("process_doctor.system.get_launchctl_jobs", return_value={"com.localfirst.x": 42}),
        patch("process_doctor.system.cpu_lookup_for", return_value=1.95),
        patch("process_doctor.system.time.time", return_value=600.0),
        patch("process_doctor.system.kill_tree") as mock_kill,
        patch("process_doctor.system.notify") as mock_notify,
    ):
        stuck = system.run_check(stuck_after_seconds=600.0, cpu_epsilon_seconds=2.0)

    assert len(stuck) == 1
    mock_kill.assert_called_once_with(42)
    mock_notify.assert_called_once()
    assert "STUCK com.localfirst.x" in (tmp_path / "doctor.log").read_text()
    assert system.load_state() == {}

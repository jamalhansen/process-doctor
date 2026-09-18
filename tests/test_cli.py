from unittest.mock import patch

from typer.testing import CliRunner

from process_doctor.cli import app
from process_doctor.core import JobState, StuckJob


def test_check_reports_ok_when_nothing_stuck():
    with patch("process_doctor.cli.system.run_check", return_value=[]):
        result = CliRunner().invoke(app, ["check"])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_check_reports_stuck_jobs():
    stuck = [StuckJob(label="com.localfirst.artist-agent", pid=123, elapsed_seconds=600.0, cpu_seconds=1.9)]
    with patch("process_doctor.cli.system.run_check", return_value=stuck):
        result = CliRunner().invoke(app, ["check"])
    assert result.exit_code == 0
    assert "STUCK" in result.output
    assert "com.localfirst.artist-agent" in result.output


def test_status_renders_a_table():
    jobs = {"com.localfirst.artist-agent": 123, "com.localfirst.photo-watcher": None}
    state = {"com.localfirst.artist-agent": JobState(pid=123, cpu_seconds=4.2, first_seen=1000.0)}
    with (
        patch("process_doctor.cli.system.get_launchctl_jobs", return_value=jobs),
        patch("process_doctor.cli.system.load_state", return_value=state),
    ):
        result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "com.localfirst.artist-agent" in result.output
    assert "com.localfirst.photo-watcher" in result.output

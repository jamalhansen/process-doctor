"""Tests for KeepAlive detection -- a persistent service's idle CPU is normal,
not the launchd-stuck signature. Found live 2026-09-19: without this, both
llm-gateway-service and http-retriever-service were being killed and
restarted by every ~15-minute poll, all day, purely for sitting idle."""
import plistlib
from unittest.mock import patch

from process_doctor import system


def _write_plist(dir_path, label, contents):
    path = dir_path / f"{label}.plist"
    with path.open("wb") as f:
        plistlib.dump(contents, f)
    return path


class TestIsKeepAlive:
    def test_keep_alive_true_is_detected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR", str(tmp_path))
        _write_plist(tmp_path, "com.localfirst.llm-gateway-service", {"KeepAlive": True})
        assert system.is_keep_alive("com.localfirst.llm-gateway-service") is True

    def test_keep_alive_absent_is_not_keep_alive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR", str(tmp_path))
        _write_plist(tmp_path, "com.localfirst.weekly-review", {"StartCalendarInterval": {}})
        assert system.is_keep_alive("com.localfirst.weekly-review") is False

    def test_keep_alive_false_is_not_keep_alive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR", str(tmp_path))
        _write_plist(tmp_path, "com.localfirst.thing", {"KeepAlive": False})
        assert system.is_keep_alive("com.localfirst.thing") is False

    def test_keep_alive_dict_form_counts_as_true(self, tmp_path, monkeypatch):
        """A conditional KeepAlive (e.g. {"SuccessfulExit": False}) still means
        launchd will restart it -- idle-until-restarted is still its normal state."""
        monkeypatch.setenv("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR", str(tmp_path))
        _write_plist(
            tmp_path, "com.localfirst.thing", {"KeepAlive": {"SuccessfulExit": False}}
        )
        assert system.is_keep_alive("com.localfirst.thing") is True

    def test_missing_plist_falls_back_to_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR", str(tmp_path))
        assert system.is_keep_alive("com.localfirst.never-existed") is False

    def test_unreadable_plist_falls_back_to_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR", str(tmp_path))
        path = tmp_path / "com.localfirst.corrupt.plist"
        path.write_text("not a plist")
        assert system.is_keep_alive("com.localfirst.corrupt") is False


class TestRunCheckExcludesKeepAliveJobs:
    def test_keep_alive_job_never_flagged_stuck_no_matter_how_flat(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROCESS_DOCTOR_STATE_PATH", str(tmp_path / "state.json"))
        monkeypatch.setenv("PROCESS_DOCTOR_LOG_PATH", str(tmp_path / "doctor.log"))
        monkeypatch.setenv("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR", str(tmp_path))
        _write_plist(tmp_path, "com.localfirst.llm-gateway-service", {"KeepAlive": True})

        from process_doctor.core import JobState

        system.save_state(
            {"com.localfirst.llm-gateway-service": JobState(pid=42, cpu_seconds=1.9, first_seen=0.0)}
        )

        with (
            patch(
                "process_doctor.system.get_launchctl_jobs",
                return_value={"com.localfirst.llm-gateway-service": 42},
            ),
            patch("process_doctor.system.cpu_lookup_for", return_value=1.95),
            patch("process_doctor.system.time.time", return_value=600.0),
        ):
            stuck = system.run_check(stuck_after_seconds=600.0, cpu_epsilon_seconds=2.0)

        assert stuck == []
        # Excluded jobs aren't tracked at all -- state naturally drops them.
        assert system.load_state() == {}

    def test_non_keep_alive_job_still_gets_flagged_stuck(self, tmp_path, monkeypatch):
        """Confirms the exclusion is specific to KeepAlive, not a blanket regression."""
        monkeypatch.setenv("PROCESS_DOCTOR_STATE_PATH", str(tmp_path / "state.json"))
        monkeypatch.setenv("PROCESS_DOCTOR_LOG_PATH", str(tmp_path / "doctor.log"))
        monkeypatch.setenv("PROCESS_DOCTOR_LAUNCH_AGENTS_DIR", str(tmp_path))
        _write_plist(tmp_path, "com.localfirst.weekly-review", {"StartCalendarInterval": {}})

        from process_doctor.core import JobState

        system.save_state(
            {"com.localfirst.weekly-review": JobState(pid=42, cpu_seconds=1.9, first_seen=0.0)}
        )

        with (
            patch(
                "process_doctor.system.get_launchctl_jobs",
                return_value={"com.localfirst.weekly-review": 42},
            ),
            patch("process_doctor.system.cpu_lookup_for", return_value=1.95),
            patch("process_doctor.system.time.time", return_value=600.0),
            patch("process_doctor.system.kill_tree"),
            patch("process_doctor.system.notify"),
        ):
            stuck = system.run_check(stuck_after_seconds=600.0, cpu_epsilon_seconds=2.0)

        assert len(stuck) == 1
        assert stuck[0].label == "com.localfirst.weekly-review"

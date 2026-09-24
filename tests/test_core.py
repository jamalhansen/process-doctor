import pytest

from process_doctor.core import (
    JobState,
    check_jobs,
    descendant_cpu_seconds,
    parse_launchctl_list,
    parse_ps_time,
)


class TestParseLaunchctlList:
    def test_filters_to_localfirst_labels(self):
        output = (
            "-\t0\tcom.localfirst.artist-agent\n"
            "1234\t0\tcom.apple.something\n"
            "5678\t0\tcom.localfirst.weekly-review\n"
            "9012\t0\tcom.jamalhansen.discovery-loop\n"
        )
        assert parse_launchctl_list(output) == {
            "com.localfirst.artist-agent": None,
            "com.localfirst.weekly-review": 5678,
            "com.jamalhansen.discovery-loop": 9012,
        }

    def test_ignores_malformed_lines(self):
        output = "not-tab-separated\ncom.localfirst.thing\n"
        assert parse_launchctl_list(output) == {}


class TestParsePsTime:
    def test_minutes_seconds(self):
        assert parse_ps_time("0:01.97") == pytest.approx(1.97)

    def test_hours_minutes_seconds(self):
        assert parse_ps_time("1:02:03") == pytest.approx(3723.0)

    def test_days_prefix(self):
        assert parse_ps_time("2-01:00:00") == pytest.approx(2 * 86400 + 3600)

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_ps_time("not-a-time")


class TestDescendantCpuSeconds:
    def test_sums_root_and_children(self):
        rows = [
            (100, 1, "0:10.00"),
            (101, 100, "0:05.00"),
            (102, 101, "0:02.00"),
            (200, 1, "9:99.00"),  # unrelated tree, must not be counted
        ]
        assert descendant_cpu_seconds(rows, 100) == pytest.approx(17.0)

    def test_missing_pid_returns_zero(self):
        assert descendant_cpu_seconds([(1, 0, "0:01.00")], 999) == 0.0


class TestCheckJobs:
    def test_new_job_is_tracked_not_flagged(self):
        next_state, stuck = check_jobs({}, {"com.localfirst.x": 42}, lambda pid: 1.0, now=1000.0)
        assert stuck == []
        assert next_state["com.localfirst.x"] == JobState(pid=42, cpu_seconds=1.0, first_seen=1000.0)

    def test_flat_cpu_past_threshold_is_stuck(self):
        prev = {"com.localfirst.x": JobState(pid=42, cpu_seconds=1.9, first_seen=0.0)}
        next_state, stuck = check_jobs(
            prev,
            {"com.localfirst.x": 42},
            lambda pid: 1.95,  # 0.05s growth over 10 minutes
            now=600.0,
            stuck_after_seconds=600.0,
            cpu_epsilon_seconds=2.0,
        )
        assert len(stuck) == 1
        assert stuck[0].label == "com.localfirst.x"
        assert stuck[0].elapsed_seconds == pytest.approx(600.0)
        assert "com.localfirst.x" not in next_state

    def test_growing_cpu_is_not_stuck_even_past_threshold(self):
        prev = {"com.localfirst.x": JobState(pid=42, cpu_seconds=1.0, first_seen=0.0)}
        next_state, stuck = check_jobs(
            prev,
            {"com.localfirst.x": 42},
            lambda pid: 40.0,  # genuinely working
            now=600.0,
            stuck_after_seconds=600.0,
            cpu_epsilon_seconds=2.0,
        )
        assert stuck == []
        assert next_state["com.localfirst.x"].cpu_seconds == 40.0
        assert next_state["com.localfirst.x"].first_seen == 0.0  # unchanged, same run

    def test_flat_cpu_before_threshold_is_not_yet_stuck(self):
        prev = {"com.localfirst.x": JobState(pid=42, cpu_seconds=1.9, first_seen=0.0)}
        next_state, stuck = check_jobs(
            prev,
            {"com.localfirst.x": 42},
            lambda pid: 1.9,
            now=60.0,
            stuck_after_seconds=600.0,
            cpu_epsilon_seconds=2.0,
        )
        assert stuck == []
        assert "com.localfirst.x" in next_state

    def test_new_pid_for_known_label_resets_tracking(self):
        prev = {"com.localfirst.x": JobState(pid=42, cpu_seconds=1.9, first_seen=0.0)}
        next_state, stuck = check_jobs(
            prev,
            {"com.localfirst.x": 99},  # relaunched under a new pid
            lambda pid: 0.1,
            now=700.0,
            stuck_after_seconds=600.0,
        )
        assert stuck == []
        assert next_state["com.localfirst.x"] == JobState(pid=99, cpu_seconds=0.1, first_seen=700.0)

    def test_not_running_job_is_dropped(self):
        prev = {"com.localfirst.x": JobState(pid=42, cpu_seconds=1.0, first_seen=0.0)}
        next_state, stuck = check_jobs(prev, {"com.localfirst.x": None}, lambda pid: 0.0, now=100.0)
        assert stuck == []
        assert next_state == {}

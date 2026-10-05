from process_doctor.data_health import DegradedTool, ToolStats, check_data_health


class TestToolStats:
    def test_failure_rate_computed(self):
        s = ToolStats(table="processing_log", tool_name="my-tool", total=10, failures=3)
        assert s.failure_rate == 0.3

    def test_failure_rate_zero_calls_is_zero_not_divide_error(self):
        s = ToolStats(table="processing_log", tool_name="my-tool", total=0, failures=0)
        assert s.failure_rate == 0.0

    def test_key_combines_table_and_tool(self):
        s = ToolStats(table="fetch_log", tool_name="my-tool", total=1, failures=0)
        assert s.key == "fetch_log:my-tool"


class TestCheckDataHealth:
    def test_below_min_calls_is_ignored_even_at_100_percent_failure(self):
        stats = [ToolStats(table="processing_log", tool_name="flaky", total=2, failures=2)]
        next_degraded, newly = check_data_health(frozenset(), stats, min_calls=5)
        assert next_degraded == frozenset()
        assert newly == []

    def test_below_threshold_is_healthy(self):
        stats = [ToolStats(table="processing_log", tool_name="mostly-fine", total=10, failures=2)]
        next_degraded, newly = check_data_health(frozenset(), stats, min_calls=5, failure_rate_threshold=0.5)
        assert next_degraded == frozenset()
        assert newly == []

    def test_at_or_above_threshold_is_newly_degraded(self):
        stats = [ToolStats(table="processing_log", tool_name="broken", total=10, failures=6)]
        next_degraded, newly = check_data_health(frozenset(), stats, min_calls=5, failure_rate_threshold=0.5)
        assert next_degraded == frozenset({"processing_log:broken"})
        assert newly == [
            DegradedTool(table="processing_log", tool_name="broken", total=10, failures=6, failure_rate=0.6)
        ]

    def test_already_degraded_does_not_alert_again(self):
        """A tool broken for days shouldn't notify on every poll -- only on the transition."""
        stats = [ToolStats(table="processing_log", tool_name="broken", total=10, failures=10)]
        previous = frozenset({"processing_log:broken"})
        next_degraded, newly = check_data_health(previous, stats, min_calls=5)
        assert next_degraded == frozenset({"processing_log:broken"})
        assert newly == []

    def test_recovery_then_re_degradation_alerts_again(self):
        stats = [ToolStats(table="processing_log", tool_name="flapping", total=10, failures=10)]
        # Not in previous (it recovered since the last poll) -- should alert fresh.
        next_degraded, newly = check_data_health(frozenset(), stats, min_calls=5)
        assert len(newly) == 1
        assert next_degraded == frozenset({"processing_log:flapping"})

    def test_multiple_tables_tracked_independently(self):
        stats = [
            ToolStats(table="processing_log", tool_name="my-tool", total=10, failures=8),
            ToolStats(table="fetch_log", tool_name="my-tool", total=10, failures=0),
        ]
        next_degraded, newly = check_data_health(frozenset(), stats, min_calls=5)
        assert next_degraded == frozenset({"processing_log:my-tool"})
        assert len(newly) == 1
        assert newly[0].table == "processing_log"

    def test_exactly_at_threshold_counts_as_degraded(self):
        stats = [ToolStats(table="processing_log", tool_name="borderline", total=10, failures=5)]
        next_degraded, _ = check_data_health(frozenset(), stats, min_calls=5, failure_rate_threshold=0.5)
        assert next_degraded == frozenset({"processing_log:borderline"})

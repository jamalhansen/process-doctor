"""Tests for the IO side of data-health checking: real DuckDB queries, state
persistence, and the full run_data_health_check orchestration."""
from unittest.mock import patch

import duckdb

from process_doctor import system
from process_doctor.data_health import ToolStats


def _seed_db(db_path, processing_rows=(), fetch_rows=(), api_call_rows=()):
    """processing_rows: (tool_name, success). fetch_rows/api_call_rows: (tool_name, success)."""
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE processing_log (tool_name VARCHAR, success BOOLEAN, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("CREATE TABLE tools (id INTEGER, name VARCHAR)")
        conn.execute(
            "CREATE TABLE fetch_log (tool_id INTEGER, success BOOLEAN, "
            "attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE api_call_log (tool_id INTEGER, success BOOLEAN, "
            "attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )

        for name, success in processing_rows:
            conn.execute(
                "INSERT INTO processing_log (tool_name, success) VALUES (?, ?)", [name, success]
            )

        tool_ids: dict[str, int] = {}
        next_id = 1
        for name, _ in list(fetch_rows) + list(api_call_rows):
            if name not in tool_ids:
                tool_ids[name] = next_id
                conn.execute("INSERT INTO tools (id, name) VALUES (?, ?)", [next_id, name])
                next_id += 1

        for name, success in fetch_rows:
            conn.execute(
                "INSERT INTO fetch_log (tool_id, success) VALUES (?, ?)",
                [tool_ids[name], success],
            )
        for name, success in api_call_rows:
            conn.execute(
                "INSERT INTO api_call_log (tool_id, success) VALUES (?, ?)",
                [tool_ids[name], success],
            )
    finally:
        conn.close()


def test_get_tool_stats_missing_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_FIRST_TRACKING_DB", str(tmp_path / "nope.duckdb"))
    assert system.get_tool_stats() == []


def test_get_tool_stats_aggregates_processing_log(tmp_path, monkeypatch):
    db = tmp_path / "test.duckdb"
    monkeypatch.setenv("LOCAL_FIRST_TRACKING_DB", str(db))
    _seed_db(
        db,
        processing_rows=[
            ("my-tool", True), ("my-tool", True), ("my-tool", False),
            ("other-tool", True),
        ],
    )
    stats = system.get_tool_stats()
    by_key = {s.key: s for s in stats}
    assert by_key["processing_log:my-tool"].total == 3
    assert by_key["processing_log:my-tool"].failures == 1
    assert by_key["processing_log:other-tool"].total == 1
    assert by_key["processing_log:other-tool"].failures == 0


def test_get_tool_stats_joins_fetch_log_and_api_call_log_through_tools(tmp_path, monkeypatch):
    db = tmp_path / "test.duckdb"
    monkeypatch.setenv("LOCAL_FIRST_TRACKING_DB", str(db))
    _seed_db(
        db,
        fetch_rows=[("retriever-user", True), ("retriever-user", False)],
        api_call_rows=[("readwise-user", False), ("readwise-user", False)],
    )
    stats = system.get_tool_stats()
    by_key = {s.key: s for s in stats}
    assert by_key["fetch_log:retriever-user"].total == 2
    assert by_key["fetch_log:retriever-user"].failures == 1
    assert by_key["api_call_log:readwise-user"].failure_rate == 1.0


def test_get_tool_stats_respects_lookback_window(tmp_path, monkeypatch):
    db = tmp_path / "test.duckdb"
    monkeypatch.setenv("LOCAL_FIRST_TRACKING_DB", str(db))
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE processing_log (tool_name VARCHAR, success BOOLEAN, created_at TIMESTAMP)"
    )
    conn.execute("CREATE TABLE tools (id INTEGER, name VARCHAR)")
    conn.execute("CREATE TABLE fetch_log (tool_id INTEGER, success BOOLEAN, attempted_at TIMESTAMP)")
    conn.execute("CREATE TABLE api_call_log (tool_id INTEGER, success BOOLEAN, attempted_at TIMESTAMP)")
    conn.execute(
        "INSERT INTO processing_log (tool_name, success, created_at) "
        "VALUES ('stale-tool', false, CURRENT_TIMESTAMP - INTERVAL 48 HOUR)"
    )
    conn.close()

    stats = system.get_tool_stats(lookback_hours=24.0)
    assert stats == []


def test_get_tool_stats_returns_empty_on_lock_conflict(tmp_path, monkeypatch):
    """Never raises -- the tracking DB is shared, best-effort infrastructure."""
    db = tmp_path / "test.duckdb"
    monkeypatch.setenv("LOCAL_FIRST_TRACKING_DB", str(db))
    _seed_db(db, processing_rows=[("my-tool", True)])

    with patch("duckdb.connect", side_effect=RuntimeError("could not set lock on file")):
        assert system.get_tool_stats() == []


def test_data_health_state_roundtrips_through_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_DOCTOR_DATA_HEALTH_STATE_PATH", str(tmp_path / "dh.json"))
    system.save_data_health_state(frozenset({"processing_log:a", "fetch_log:b"}))
    assert system.load_data_health_state() == frozenset({"processing_log:a", "fetch_log:b"})


def test_load_data_health_state_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_DOCTOR_DATA_HEALTH_STATE_PATH", str(tmp_path / "nope.json"))
    assert system.load_data_health_state() == frozenset()


def test_run_data_health_check_notifies_and_logs_on_newly_degraded(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_DOCTOR_DATA_HEALTH_STATE_PATH", str(tmp_path / "dh.json"))
    monkeypatch.setenv("PROCESS_DOCTOR_LOG_PATH", str(tmp_path / "doctor.log"))

    broken = ToolStats(table="processing_log", tool_name="broken-tool", total=10, failures=10)
    with (
        patch("process_doctor.system.get_tool_stats", return_value=[broken]),
        patch("process_doctor.system.notify") as mock_notify,
    ):
        newly = system.run_data_health_check()

    assert len(newly) == 1
    assert newly[0].tool_name == "broken-tool"
    mock_notify.assert_called_once()
    assert "broken-tool" in mock_notify.call_args.args[1]
    assert "DEGRADED broken-tool" in (tmp_path / "doctor.log").read_text()
    assert system.load_data_health_state() == frozenset({"processing_log:broken-tool"})


def test_run_data_health_check_does_not_renotify_already_degraded(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESS_DOCTOR_DATA_HEALTH_STATE_PATH", str(tmp_path / "dh.json"))
    monkeypatch.setenv("PROCESS_DOCTOR_LOG_PATH", str(tmp_path / "doctor.log"))
    system.save_data_health_state(frozenset({"processing_log:broken-tool"}))

    broken = ToolStats(table="processing_log", tool_name="broken-tool", total=10, failures=10)
    with (
        patch("process_doctor.system.get_tool_stats", return_value=[broken]),
        patch("process_doctor.system.notify") as mock_notify,
    ):
        newly = system.run_data_health_check()

    assert newly == []
    mock_notify.assert_not_called()


def test_run_data_health_check_never_kills_anything(tmp_path, monkeypatch):
    """A degraded tool's process is running fine -- killing it wouldn't fix a config problem."""
    monkeypatch.setenv("PROCESS_DOCTOR_DATA_HEALTH_STATE_PATH", str(tmp_path / "dh.json"))
    monkeypatch.setenv("PROCESS_DOCTOR_LOG_PATH", str(tmp_path / "doctor.log"))

    broken = ToolStats(table="processing_log", tool_name="broken-tool", total=10, failures=10)
    with (
        patch("process_doctor.system.get_tool_stats", return_value=[broken]),
        patch("process_doctor.system.notify"),
        patch("process_doctor.system.kill_tree") as mock_kill,
    ):
        system.run_data_health_check()

    mock_kill.assert_not_called()

# process-doctor

Watches every com.localfirst.* LaunchAgent for a hang and kills, logs, and notifies.

Two signals, since 2026-10-03:

- **Heartbeat** -- a job that calls `local_first_common.heartbeat.heartbeat()` per unit of work is stuck when its last beat is older than `stuck_after`. Its CPU is ignored. This is the right signal for jobs that mostly wait (LLM calls, fetches).
- **Flat CPU** -- a job that never heartbeats is stuck after `stuck_after` seconds under one pid with under `cpu_epsilon` seconds of CPU growth. The original heuristic; it misread the discovery run's 16 s-per-item gateway waits as a hang and killed it mid-scoring most mornings from 2026-09-30.

Either way, `max_runtime` is a hard ceiling. Per-job limits live in `~/.config/local-first/process-doctor.toml`:

```toml
[jobs."com.localfirst.discovery-loop"]
stuck_after = 1800
max_runtime = 7200
```

For a job to heartbeat, its LaunchAgent plist sets `LOCALFIRST_JOB_LABEL` to its own label; `heartbeat()` is a no-op without it. `process-doctor status` shows each job's last beat; the log line on a kill carries `reason=` (`flat-cpu`, `stale-heartbeat`, `max-runtime`).

## Quickstart

```bash
uv run process-doctor
```


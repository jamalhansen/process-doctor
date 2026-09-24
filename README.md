# process-doctor

Watches every com.localfirst.* and com.jamalhansen.* LaunchAgent for the launchd-stuck signature (near-zero CPU growth while still running) and kills, logs, and notifies on a hang.

## Quickstart

```bash
uv run process-doctor
```

## Status

Scaffolded via `local-first-common/scripts/new_tool.py` -- replace `core.run()`
and the CLI options in `cli.py` with the tool's real logic.

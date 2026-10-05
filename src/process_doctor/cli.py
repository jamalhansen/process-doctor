from typing import Annotated

import typer
from local_first_common.tracking import register_tool
from rich.console import Console
from rich.table import Table

from . import system
from .system import TOOL_NAME

_TOOL = register_tool(TOOL_NAME)

console = Console(stderr=True)
app = typer.Typer(
    help="Watches every com.localfirst.* LaunchAgent for a hang -- a stopped "
    "heartbeat for jobs that send one, flat CPU for jobs that don't -- and "
    "kills, logs, and notifies. Per-job limits: [jobs.<label>] in "
    "~/.config/local-first/process-doctor.toml."
)


@app.command()
def check(
    stuck_after: Annotated[
        float,
        typer.Option(
            help="Seconds without a heartbeat (or with flat CPU) before a job is judged stuck; "
            "a [jobs.<label>] stuck_after overrides it per job"
        ),
    ] = 600.0,
    cpu_epsilon: Annotated[
        float,
        typer.Option(help="CPU-seconds of growth below which a job counts as flat"),
    ] = 2.0,
    data_health: Annotated[
        bool,
        typer.Option(
            "--data-health/--no-data-health",
            help="Also check processing_log/fetch_log/api_call_log for tools "
            "that are running but silently failing every call",
        ),
    ] = True,
    lookback_hours: Annotated[
        float,
        typer.Option(help="Data-health lookback window"),
    ] = 24.0,
    min_calls: Annotated[
        int,
        typer.Option(help="Minimum calls in the window before a tool's rate is judged"),
    ] = 5,
    failure_rate_threshold: Annotated[
        float,
        typer.Option(help="Failure rate (0-1) at or above which a tool counts as degraded"),
    ] = 0.5,
) -> None:
    """Run one detection pass. Safe to call repeatedly from a LaunchAgent StartInterval."""
    stuck = system.run_check(stuck_after_seconds=stuck_after, cpu_epsilon_seconds=cpu_epsilon)
    if stuck:
        for job in stuck:
            console.print(
                f"[red]STUCK[/red] {job.label} (pid {job.pid}, {job.elapsed_seconds:.0f}s, {job.reason}) -- killed"
            )
    else:
        console.print("[green]OK[/green] no stuck jobs")

    if data_health:
        degraded = system.run_data_health_check(
            lookback_hours=lookback_hours,
            min_calls=min_calls,
            failure_rate_threshold=failure_rate_threshold,
        )
        if degraded:
            for tool in degraded:
                console.print(
                    f"[yellow]DEGRADED[/yellow] {tool.tool_name} ({tool.table}) "
                    f"{tool.failure_rate:.0%} failing ({tool.failures}/{tool.total})"
                )
        else:
            console.print("[green]OK[/green] no newly degraded tools")


@app.command()
def status() -> None:
    """Show currently tracked com.localfirst.* jobs and their state."""
    jobs = system.get_launchctl_jobs()
    state = system.load_state()
    # Short headers: the full label must fit in an 80-column terminal without truncation.
    table = Table("label", "pid", "cpu s", "since (epoch)", "beat")
    now = system.time.time()
    for label, pid in sorted(jobs.items()):
        js = state.get(label)
        beat = system.last_heartbeat(label)
        table.add_row(
            label,
            str(pid) if pid else "-",
            f"{js.cpu_seconds:.1f}" if js else "-",
            f"{js.first_seen:.0f}" if js else "-",
            f"{(now - beat) / 60:.0f} min ago" if beat else "-",
        )
    console.print(table)


@app.command("data-health")
def data_health_status(
    lookback_hours: Annotated[
        float,
        typer.Option(help="Data-health lookback window"),
    ] = 24.0,
    min_calls: Annotated[
        int,
        typer.Option(help="Minimum calls in the window before a tool's rate is shown"),
    ] = 5,
) -> None:
    """Show every tool's recent call success rate, without alerting or changing state."""
    stats = system.get_tool_stats(lookback_hours=lookback_hours)
    table = Table("table", "tool", "calls", "failures", "failure rate")
    for s in sorted(stats, key=lambda s: (-s.failure_rate, s.tool_name)):
        if s.total < min_calls:
            continue
        style = "red" if s.failure_rate >= 0.5 else None
        table.add_row(
            s.table,
            s.tool_name,
            str(s.total),
            str(s.failures),
            f"{s.failure_rate:.0%}",
            style=style,
        )
    console.print(table)


if __name__ == "__main__":
    app()

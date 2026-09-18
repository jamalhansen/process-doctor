from typing import Annotated

import typer
from local_first_common.tracking import register_tool
from rich.console import Console
from rich.table import Table

from . import system

TOOL_NAME = "process-doctor"
_TOOL = register_tool(TOOL_NAME)

console = Console(stderr=True)
app = typer.Typer(
    help="Watches every com.localfirst.* LaunchAgent for the launchd-stuck "
    "signature (near-zero CPU growth while still running) and kills, logs, "
    "and notifies on a hang."
)


@app.command()
def check(
    stuck_after: Annotated[
        float,
        typer.Option(help="Seconds a job can run with flat CPU before it's judged stuck"),
    ] = 600.0,
    cpu_epsilon: Annotated[
        float,
        typer.Option(help="CPU-seconds of growth below which a job counts as flat"),
    ] = 2.0,
) -> None:
    """Run one detection pass. Safe to call repeatedly from a LaunchAgent StartInterval."""
    stuck = system.run_check(stuck_after_seconds=stuck_after, cpu_epsilon_seconds=cpu_epsilon)
    if stuck:
        for job in stuck:
            console.print(
                f"[red]STUCK[/red] {job.label} (pid {job.pid}, "
                f"{job.elapsed_seconds:.0f}s flat) -- killed"
            )
    else:
        console.print("[green]OK[/green] no stuck jobs")


@app.command()
def status() -> None:
    """Show currently tracked com.localfirst.* jobs and their state."""
    jobs = system.get_launchctl_jobs()
    state = system.load_state()
    table = Table("label", "pid", "cpu_seconds", "tracked since (epoch)")
    for label, pid in sorted(jobs.items()):
        js = state.get(label)
        table.add_row(
            label,
            str(pid) if pid else "-",
            f"{js.cpu_seconds:.1f}" if js else "-",
            f"{js.first_seen:.0f}" if js else "-",
        )
    console.print(table)


if __name__ == "__main__":
    app()

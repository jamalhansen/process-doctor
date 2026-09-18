from typing import Annotated

import typer
from local_first_common.cli import (
    dry_run_option,
    model_option,
    no_llm_option,
    provider_option,
    resolve_dry_run,
)
from local_first_common.tracking import register_tool
from rich.console import Console

from .core import run

TOOL_NAME = "process-doctor"
_TOOL = register_tool(TOOL_NAME)

console = Console(stderr=True)
app = typer.Typer(help="Watches every com.localfirst.* LaunchAgent for the launchd-stuck signature (near-zero CPU growth while still running) and kills, logs, and notifies on a hang.")


@app.command()
def main(
    provider: Annotated[str, provider_option()] = "ollama",
    model: Annotated[str | None, model_option()] = None,
    dry_run: Annotated[bool, dry_run_option()] = False,
    no_llm: Annotated[bool, no_llm_option()] = False,
) -> None:
    """Watches every com.localfirst.* LaunchAgent for the launchd-stuck signature (near-zero CPU growth while still running) and kills, logs, and notifies on a hang."""
    dry_run = resolve_dry_run(dry_run, no_llm)
    result = run(dry_run=dry_run)
    console.print(result)


if __name__ == "__main__":
    app()

"""Command-line entry point: ``msa``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from msa import __version__
from msa.core.graph import analyze, render_markdown
from msa.core.transitions import TABLE
from msa.core.values import Phase

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Multistep coding agent.")
fsm_app = typer.Typer(no_args_is_help=True, help="Inspect the orchestrator state machine.")
app.add_typer(fsm_app, name="fsm")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@fsm_app.command()
def diagram(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write to this file instead of stdout.")
    ] = None,
) -> None:
    """Render the state machine as Markdown with a Mermaid diagram."""
    text = render_markdown(TABLE)
    if output is None:
        typer.echo(text, nl=False)
        return
    output.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {output}")


@fsm_app.command()
def check() -> None:
    """Statically verify the transition table: reachability, liveness, terminal shape."""
    problems = analyze(TABLE)
    for problem in problems:
        typer.echo(f"error: {problem}", err=True)
    if problems:
        raise typer.Exit(code=1)
    typer.echo(f"ok: {len(Phase)} phases, {len(TABLE)} rules, every live phase can reach DONE")


def main() -> None:
    app()

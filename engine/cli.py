"""ontorag-playground CLI."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer
import uvicorn

app = typer.Typer(help="ontorag-playground — domain-neutral ontology chatbot")


@app.command()
def serve(
    port: int = typer.Option(int(os.environ.get("PORT", 8200)), help="HTTP port"),
    reload: bool = typer.Option(False, help="Auto-reload on file changes"),
):
    """Start the playground server."""
    uvicorn.run(
        "engine.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=reload,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


@app.command()
def init():
    """Copy .env.example → .env if not present."""
    src = Path(".env.example")
    dst = Path(".env")
    if dst.exists():
        typer.echo(".env already exists, skipping.")
        return
    if not src.exists():
        typer.echo("No .env.example found.", err=True)
        raise typer.Exit(1)
    dst.write_text(src.read_text())
    typer.echo("Created .env — edit it before starting.")


@app.command()
def check():
    """Domain-neutrality check: grep engine/ for domain-specific words."""
    result = subprocess.run(
        ["grep", "-r", "--include=*.py", "-l",
         "--exclude=cli.py",
         "-E", r"\b(movie|actor|director|tmdb|genre)\b|영화|감독|배우|장르",
         "engine/"],
        capture_output=True, text=True,
    )
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    if hits:
        typer.echo("⚠  Domain words found in engine/:\n" + "\n".join(f"  {h}" for h in hits), err=True)
        raise typer.Exit(1)
    typer.echo("✓ engine/ is domain-neutral (0 domain words found)")


if __name__ == "__main__":
    app()

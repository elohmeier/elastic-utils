"""List commands for Elasticsearch resources (kubectl-style)."""

from __future__ import annotations

import json
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from .client import ElasticsearchClient

console = Console()


def _output_json(data: list[dict[str, Any]]) -> None:
    """Output data as JSON."""
    console.print(json.dumps(data, indent=2))


def _format_docs(count: str | None) -> str:
    """Format document count with thousands separator."""
    if not count:
        return "-"
    try:
        return f"{int(count):,}"
    except ValueError:
        return count


@click.group()
def get() -> None:
    """List Elasticsearch resources."""
    pass


@get.command()
@click.argument("pattern", required=False)
@click.option(
    "-o",
    "--output",
    type=click.Choice(["table", "wide", "json"]),
    default="table",
    help="Output format.",
)
@click.option(
    "--sort",
    default="creation.date",
    help="Sort by column (default: creation.date).",
)
def indices(pattern: str | None, output: str, sort: str) -> None:
    """List indices.

    PATTERN is an optional index pattern (e.g., 'logs-*', '*frozen*').
    """
    client = ElasticsearchClient.from_credentials(console)

    if output == "wide":
        headers = "index,health,status,docs.count,store.size,pri,rep,creation.date"
    else:
        headers = "index,health,status,docs.count,store.size,creation.date"

    data = client.cat_indices(pattern, sort=sort, headers=headers)

    if output == "json":
        _output_json(data)
        return

    if not data:
        console.print("[yellow]No indices found.[/yellow]")
        return

    table = Table(box=None)
    table.add_column("NAME", style="bold")
    table.add_column("HEALTH")
    table.add_column("STATUS")
    table.add_column("DOCS", justify="right")
    table.add_column("SIZE", justify="right")
    if output == "wide":
        table.add_column("PRI", justify="right")
        table.add_column("REP", justify="right")
    table.add_column("CREATED")

    for idx in data:
        health = idx.get("health", "")
        health_style = {
            "green": "green",
            "yellow": "yellow",
            "red": "red",
        }.get(health, "")

        row = [
            idx.get("index", ""),
            f"[{health_style}]{health}[/{health_style}]" if health_style else health,
            idx.get("status", ""),
            _format_docs(idx.get("docs.count")),
            idx.get("store.size", "-"),
        ]
        if output == "wide":
            row.extend([idx.get("pri", "-"), idx.get("rep", "-")])
        row.append(idx.get("creation.date", "-"))
        table.add_row(*row)

    console.print(table)


@get.command()
@click.argument("pattern", required=False)
@click.option(
    "-o",
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format.",
)
def aliases(pattern: str | None, output: str) -> None:
    """List aliases.

    PATTERN is an optional alias pattern (e.g., 'logs-*').
    """
    client = ElasticsearchClient.from_credentials(console)
    data = client.cat_aliases(pattern)

    if output == "json":
        _output_json(data)
        return

    if not data:
        console.print("[yellow]No aliases found.[/yellow]")
        return

    table = Table(box=None)
    table.add_column("ALIAS", style="bold")
    table.add_column("INDEX")
    table.add_column("FILTER")
    table.add_column("ROUTING.INDEX")
    table.add_column("ROUTING.SEARCH")

    for alias in data:
        table.add_row(
            alias.get("alias", ""),
            alias.get("index", ""),
            alias.get("filter", "-"),
            alias.get("routing.index", "-"),
            alias.get("routing.search", "-"),
        )

    console.print(table)

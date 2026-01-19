"""Describe commands for Elasticsearch resources (kubectl-style)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.table import Table

from .client import ElasticsearchClient

console = Console()


def _format_timestamp(ts_ms: int | str | None) -> str:
    """Format a millisecond timestamp to ISO format."""
    if ts_ms is None:
        return "-"
    try:
        ts = int(ts_ms) / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError):
        return str(ts_ms)


def _format_duration(start: str | None, end: str | None) -> str:
    """Format duration between two ISO timestamps."""
    if not start or not end:
        return "-"
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        delta = end_dt - start_dt
        days = delta.days
        if days > 0:
            return f"{days} day{'s' if days != 1 else ''}"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        minutes = delta.seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    except (ValueError, TypeError):
        return "-"


@click.group()
def describe() -> None:
    """Show detailed resource information."""
    pass


@describe.command()
@click.argument("name")
@click.option(
    "-o",
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.option(
    "--timestamp-field",
    default="@timestamp",
    help="Field to use for date range calculation.",
)
def index(name: str, output: str, timestamp_field: str) -> None:
    """Show detailed index information.

    NAME is the index name.
    """
    client = ElasticsearchClient.from_credentials(console)

    with client.session():
        # Get index info from cat API
        cat_data = client.cat_indices(
            name,
            headers="index,health,status,docs.count,store.size,pri,rep,creation.date",
        )

        # Get settings
        settings = client.get_index_settings(name)

        # Get ILM status
        ilm_data = client.ilm_explain(name)

        # Get date range
        min_date, max_date = client.get_date_range(name, timestamp_field)

    if output == "json":
        result = {
            "index": cat_data[0].model_dump(by_alias=True) if cat_data else {},
            "settings": settings,
            "ilm": ilm_data.model_dump(),
            "date_range": {"min": min_date, "max": max_date},
        }
        console.print(json.dumps(result, indent=2))
        return

    if not cat_data:
        console.print(f"[red]Index not found: {name}[/red]")
        raise SystemExit(1)

    idx = cat_data[0]

    # Basic info
    console.print(f"[bold]Name:[/bold]         {idx.index or name}")
    health = idx.health or ""
    health_style = {"green": "green", "yellow": "yellow", "red": "red"}.get(health, "")
    if health_style:
        console.print(
            f"[bold]Health:[/bold]       [{health_style}]{health}[/{health_style}]"
        )
    else:
        console.print(f"[bold]Health:[/bold]       {health}")
    console.print(f"[bold]Status:[/bold]       {idx.status or '-'}")
    console.print(f"[bold]Docs:[/bold]         {idx.docs_count or '-'}")
    console.print(f"[bold]Size:[/bold]         {idx.store_size or '-'}")
    console.print(
        f"[bold]Shards:[/bold]       {idx.pri or '-'} primary, {idx.rep or '-'} replica"
    )
    console.print(f"[bold]Created:[/bold]      {idx.creation_date or '-'}")

    # Date range
    console.print()
    console.print("[bold]Date Range:[/bold]")
    console.print(f"  Oldest:       {min_date or '-'}")
    console.print(f"  Newest:       {max_date or '-'}")
    console.print(f"  Span:         {_format_duration(min_date, max_date)}")

    # ILM status
    if ilm_data.indices:
        console.print()
        console.print("[bold]ILM Status:[/bold]")
        for idx_name, ilm_info in ilm_data.indices.items():
            if ilm_info.managed:
                console.print(f"  Phase:        {ilm_info.phase or '-'}")
                console.print(f"  Action:       {ilm_info.action or '-'}")
                console.print(f"  Step:         {ilm_info.step or '-'}")
                console.print(f"  Age:          {ilm_info.age or '-'}")
            else:
                console.print("  Not managed by ILM")


@describe.command()
@click.argument("name")
@click.option(
    "-o",
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.option(
    "--timestamp-field",
    default="@timestamp",
    help="Field to use for date range calculation.",
)
def alias(name: str, output: str, timestamp_field: str) -> None:
    """Show detailed alias information.

    NAME is the alias name.
    """
    client = ElasticsearchClient.from_credentials(console)

    with client.session():
        # Get alias details (which indices are in it)
        alias_data = client.get_alias(name)

        # Get index info for member indices
        cat_data = client.cat_indices(
            name,
            headers="index,health,status,docs.count,store.size,creation.date",
        )

        # Get ILM status for all indices in alias
        ilm_data = client.ilm_explain(name)

        # Get overall date range across the alias
        min_date, max_date = client.get_date_range(name, timestamp_field)

    if output == "json":
        result = {
            "alias": name,
            "indices": {k: v.model_dump() for k, v in alias_data.items()},
            "index_info": [idx.model_dump(by_alias=True) for idx in cat_data],
            "ilm": ilm_data.model_dump(),
            "date_range": {"min": min_date, "max": max_date},
        }
        console.print(json.dumps(result, indent=2))
        return

    index_names = list(alias_data.keys())

    console.print(f"[bold]Name:[/bold]         {name}")
    console.print(f"[bold]Indices:[/bold]      {len(index_names)}")

    # Index table
    if cat_data:
        console.print()
        table = Table(box=None, title="Member Indices")
        table.add_column("INDEX", style="bold")
        table.add_column("HEALTH")
        table.add_column("DOCS", justify="right")
        table.add_column("SIZE", justify="right")
        table.add_column("CREATED")

        for idx in cat_data:
            health = idx.health or ""
            health_style = {"green": "green", "yellow": "yellow", "red": "red"}.get(
                health, ""
            )
            table.add_row(
                idx.index or "",
                f"[{health_style}]{health}[/{health_style}]"
                if health_style
                else health,
                idx.docs_count or "-",
                idx.store_size or "-",
                idx.creation_date or "-",
            )

        console.print(table)

    # Date range
    console.print()
    console.print("[bold]Date Range:[/bold]")
    console.print(f"  Oldest:       {min_date or '-'}")
    console.print(f"  Newest:       {max_date or '-'}")
    console.print(f"  Span:         {_format_duration(min_date, max_date)}")

    # ILM status
    managed_indices = [
        (idx_name, info) for idx_name, info in ilm_data.indices.items() if info.managed
    ]

    if managed_indices:
        console.print()
        ilm_table = Table(box=None, title="ILM Status")
        ilm_table.add_column("INDEX", style="bold")
        ilm_table.add_column("PHASE")
        ilm_table.add_column("ACTION")
        ilm_table.add_column("STEP")
        ilm_table.add_column("AGE")

        for idx_name, ilm_info in managed_indices:
            ilm_table.add_row(
                idx_name,
                ilm_info.phase or "-",
                ilm_info.action or "-",
                ilm_info.step or "-",
                ilm_info.age or "-",
            )

        console.print(ilm_table)

"""Version command for Elasticsearch cluster info."""

from __future__ import annotations

import json

import click
from rich.console import Console

from .client import ElasticsearchClient

console = Console()


@click.command()
@click.option(
    "-o",
    "--output",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
def version(output: str) -> None:
    """Show Elasticsearch cluster version."""
    client = ElasticsearchClient.from_credentials(console)
    info = client.cluster_info()

    if output == "json":
        console.print(json.dumps(info.model_dump(), indent=2))
        return

    console.print(f"[bold]Cluster:[/bold]      {info.cluster_name}")
    console.print(f"[bold]UUID:[/bold]         {info.cluster_uuid}")
    console.print(f"[bold]Version:[/bold]      {info.version.number}")
    console.print(
        f"[bold]Build:[/bold]        {info.version.build_flavor} / {info.version.build_type}"
    )
    console.print(f"[bold]Lucene:[/bold]       {info.version.lucene_version}")

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
        console.print(json.dumps(info, indent=2))
        return

    version_info = info.get("version", {})
    console.print(f"[bold]Cluster:[/bold]      {info.get('cluster_name', '-')}")
    console.print(f"[bold]UUID:[/bold]         {info.get('cluster_uuid', '-')}")
    console.print(f"[bold]Version:[/bold]      {version_info.get('number', '-')}")
    console.print(
        f"[bold]Build:[/bold]        {version_info.get('build_flavor', '-')} / {version_info.get('build_type', '-')}"
    )
    console.print(
        f"[bold]Lucene:[/bold]       {version_info.get('lucene_version', '-')}"
    )

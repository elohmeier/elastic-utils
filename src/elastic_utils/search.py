"""Search commands for Elasticsearch async search and export."""

import json
import sys
import time
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .client import ElasticsearchClient
from .formatting import format_hits, format_shards, write_output

console = Console()


def read_query(query_file: Path | None) -> dict[str, Any]:
    """Read query from file or stdin."""
    if query_file:
        content = query_file.read_text()
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
        if not content.strip():
            console.print("[red]No query provided.[/red]")
            console.print(
                "Provide a query file with --query-file or pipe JSON via stdin."
            )
            raise SystemExit(1)
    else:
        console.print("[red]No query provided.[/red]")
        console.print("Provide a query file with --query-file or pipe JSON via stdin.")
        raise SystemExit(1)

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON:[/red] {e}")
        raise SystemExit(1)


@click.group()
def search() -> None:
    """Run async searches and export results from Elasticsearch."""
    pass


@search.command()
@click.option(
    "--index",
    required=True,
    help="Index or alias to search",
)
@click.option(
    "--query-file",
    type=click.Path(exists=True, path_type=Path),
    help="Path to JSON file containing the query",
)
@click.option(
    "--wait-for",
    default="1s",
    help="Initial wait timeout for completion (default: 1s)",
)
@click.option(
    "--keep-alive",
    default="1h",
    help="How long to keep the search alive (default: 1h)",
)
def submit(index: str, query_file: Path | None, wait_for: str, keep_alive: str) -> None:
    """Submit an async search and return the search ID."""
    client = ElasticsearchClient.from_credentials(console)
    query = read_query(query_file)

    console.print(f"Submitting async search to [bold]{index}[/bold]...")

    result = client.async_search_submit(
        index, query, wait_for=wait_for, keep_alive=keep_alive
    )

    console.print("[green]Search submitted![/green]")
    console.print(f"  Search ID: [bold]{result.id}[/bold]")
    console.print(f"  Running: {result.is_running}")
    console.print(f"  Partial: {result.is_partial}")
    console.print(f"  Shards: {format_shards(result.response.shards)}")


@search.command()
@click.argument("search_id")
@click.option(
    "--wait-for",
    default=None,
    help="Wait timeout for completion (e.g., 5s)",
)
def status(search_id: str, wait_for: str | None) -> None:
    """Check the status of an async search."""
    client = ElasticsearchClient.from_credentials(console)

    result = client.async_search_status(search_id, wait_for=wait_for)

    status_color = "yellow" if result.is_running else "green"
    console.print(
        f"[{status_color}]Status: {'Running' if result.is_running else 'Complete'}[/{status_color}]"
    )
    console.print(f"  Partial: {result.is_partial}")
    console.print(f"  Shards: {format_shards(result.response.shards)}")
    console.print(f"  Took: {result.response.took}ms")
    console.print(f"  Hits returned: {result.total_hits}")


@search.command()
@click.argument("search_id")
@click.option(
    "--interval",
    default=5,
    type=int,
    help="Poll interval in seconds (default: 5)",
)
@click.option(
    "--timeout",
    default=None,
    type=int,
    help="Maximum wait time in seconds (optional)",
)
def wait(search_id: str, interval: int, timeout: int | None) -> None:
    """Wait for an async search to complete, showing progress."""
    client = ElasticsearchClient.from_credentials(console)

    start_time = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} shards"),
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("", total=None)

        while True:
            result = client.async_search_poll(search_id)
            if result is None:
                break

            shards = result.response.shards
            progress.update(
                task,
                total=shards.total,
                completed=shards.successful,
                description=f"(skipped: {shards.skipped}, failed: {shards.failed})",
            )

            if not result.is_running:
                break

            if timeout and (time.time() - start_time) >= timeout:
                console.print("[yellow]Timeout reached, search still running.[/yellow]")
                raise SystemExit(1)

            time.sleep(interval)

    # Final status (result is from last poll)
    if result:
        console.print("[green]Search complete![/green]")
        console.print(f"  Shards: {format_shards(result.response.shards)}")
        console.print(f"  Took: {result.response.took}ms")
        console.print(f"  Hits returned: {result.total_hits}")


@search.command()
@click.argument("search_id")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: stdout)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "jsonl"]),
    default="jsonl",
    help="Output format (default: jsonl)",
)
def get(search_id: str, output: Path | None, output_format: str) -> None:
    """Get the results of an async search."""
    client = ElasticsearchClient.from_credentials(console)

    result = client.async_search_status(search_id)

    formatted = format_hits(result.hits, output_format)
    write_output(
        formatted,
        output,
        console,
        success_message=f"[green]Wrote {result.total_hits} hits to {output}[/green]"
        if output
        else None,
    )


@search.command()
@click.argument("search_id")
def delete(search_id: str) -> None:
    """Delete an async search."""
    client = ElasticsearchClient.from_credentials(console)

    deleted = client.async_search_delete(search_id, warn_not_found=True)

    if deleted:
        console.print("[green]Search deleted.[/green]")


@search.command()
@click.option(
    "--index",
    required=True,
    help="Index or alias to search",
)
@click.option(
    "--query-file",
    type=click.Path(exists=True, path_type=Path),
    help="Path to JSON file containing the query",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: stdout)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "jsonl"]),
    default="jsonl",
    help="Output format (default: jsonl)",
)
@click.option(
    "--page-size",
    default=1000,
    type=int,
    help="Results per page (default: 1000)",
)
@click.option(
    "--keep-alive",
    default="10m",
    help="PIT keep-alive duration (default: 10m)",
)
@click.option(
    "--from-date",
    help="Start date filter (ISO format, e.g., 2025-01-01)",
)
@click.option(
    "--to-date",
    help="End date filter (ISO format, e.g., 2025-02-01)",
)
def export(
    index: str,
    query_file: Path | None,
    output: Path | None,
    output_format: str,
    page_size: int,
    keep_alive: str,
    from_date: str | None,
    to_date: str | None,
) -> None:
    """Export all search results using async search + PIT pagination."""
    client = ElasticsearchClient.from_credentials(console)
    query = read_query(query_file)

    # Add time range filter if specified
    if from_date or to_date:
        if "query" not in query:
            query["query"] = {"bool": {"filter": []}}
        if "bool" not in query["query"]:
            query["query"] = {"bool": {"must": [query["query"]], "filter": []}}
        if "filter" not in query["query"]["bool"]:
            query["query"]["bool"]["filter"] = []

        range_filter: dict[str, Any] = {"range": {"@timestamp": {}}}
        if from_date:
            range_filter["range"]["@timestamp"]["gte"] = from_date
        if to_date:
            range_filter["range"]["@timestamp"]["lt"] = to_date
        query["query"]["bool"]["filter"].append(range_filter)

    # Ensure proper sort for pagination (without _shard_doc for non-PIT queries)
    if "sort" not in query:
        query["sort"] = [{"@timestamp": "asc"}]

    # Set page size
    query["size"] = page_size

    console.print(f"[bold]Starting export from {index}[/bold]")

    # Use session for connection pooling during multi-request export
    with client.session():
        # Step 1: Run async search to verify query works on frozen indices
        console.print("Running initial async search...")
        initial_result = client.async_search_submit(
            index, query, wait_for="1s", keep_alive="1h"
        )
        async_search_id = initial_result.id

        # Step 2: Wait for async search to complete
        console.print("Waiting for async search to complete...")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} shards"),
            TimeElapsedColumn(),
            TextColumn("eta"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("", total=None)

            while True:
                result = client.async_search_poll(async_search_id)
                if result is None:
                    break

                shards = result.response.shards
                progress.update(
                    task,
                    total=shards.total,
                    completed=shards.successful,
                    description=f"(skipped: {shards.skipped}, failed: {shards.failed})",
                )

                if not result.is_running:
                    break

                time.sleep(5)

        initial_hits = result.hits if result else []
        console.print(
            f"Initial search complete, got {len(initial_hits)} hits in first page"
        )

        # Cleanup async search
        client.async_search_delete(async_search_id, silent=True)

        # Step 3: Open PIT for pagination
        console.print("Opening Point-in-Time for pagination...")
        pit_id = client.open_pit(index, keep_alive=keep_alive)

        # Step 4: Paginate through all results
        all_hits: list[dict[str, Any]] = []
        search_after = None
        page = 0

        # Prepare query for PIT search (add _shard_doc tiebreaker for pagination)
        pit_query = query.copy()
        pit_query["pit"] = {"id": pit_id, "keep_alive": keep_alive}
        # Add _shard_doc tiebreaker for efficient pagination with PIT
        pit_query["sort"] = query.get("sort", [{"@timestamp": "asc"}]) + [
            {"_shard_doc": "asc"}
        ]

        console.print("Fetching all pages...")
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("Fetching...", total=None)

                while True:
                    page += 1
                    if search_after:
                        pit_query["search_after"] = search_after

                    progress.update(
                        task, description=f"Page {page} | Total docs: {len(all_hits)}"
                    )

                    search_result = client.search_with_pit(pit_query)
                    hits = search_result.hit_list

                    if not hits:
                        break

                    all_hits.extend(hits)
                    search_after = hits[-1].get("sort")

                    # Refresh PIT keep-alive
                    if search_result.pit_id:
                        pit_query["pit"]["id"] = search_result.pit_id
        finally:
            # Step 5: Close PIT
            client.close_pit(pit_id)

    console.print(f"[green]Export complete! Total documents: {len(all_hits)}[/green]")

    # Write output
    formatted = format_hits(all_hits, output_format)
    write_output(
        formatted,
        output,
        console,
        success_message=f"Wrote to {output}" if output else None,
    )

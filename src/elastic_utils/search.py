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
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .client import ElasticsearchClient
from .formatting import format_hits, format_shards, write_output

console = Console()


def create_client(
    *,
    url: str | None = None,
    api_key_id: str | None = None,
    api_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> ElasticsearchClient:
    """Create client from explicit auth options or stored credentials."""
    return ElasticsearchClient.from_auth_or_credentials(
        url=url,
        api_key_id=api_key_id,
        api_key=api_key,
        username=username,
        password=password,
        console=console,
    )


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
    """Run async searches and transfer results via export/import."""
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

    hits = result.hits
    formatted = format_hits(hits, output_format)
    write_output(
        formatted,
        output,
        console,
        success_message=f"[green]Wrote {len(hits)} hits to {output}[/green]"
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
@click.option("--url", help="Source Elasticsearch URL (overrides stored credentials)")
@click.option("--api-key-id", help="Source API key ID")
@click.option("--api-key", help="Source API key value")
@click.option("--username", help="Source username (basic auth)")
@click.option("--password", help="Source password (basic auth)")
def export(
    index: str,
    query_file: Path | None,
    output: Path | None,
    output_format: str,
    page_size: int,
    keep_alive: str,
    from_date: str | None,
    to_date: str | None,
    url: str | None,
    api_key_id: str | None,
    api_key: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Export all search results using async search + PIT pagination."""
    client = create_client(
        url=url,
        api_key_id=api_key_id,
        api_key=api_key,
        username=username,
        password=password,
    )
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

        total_docs = result.total_hits if result else 0
        console.print(f"Initial search complete, total matching docs: {total_docs:,}")

        # Cleanup async search
        client.async_search_delete(async_search_id, silent=True)

        # Step 3: Open PIT for pagination
        console.print("Opening Point-in-Time for pagination...")
        pit_id = client.open_pit(index, keep_alive=keep_alive)

        # Step 4: Paginate through all results
        all_hits: list[dict[str, Any]] = []
        search_after = None
        page = 0
        docs_written = 0

        # Prepare query for PIT search (add _shard_doc tiebreaker for pagination)
        pit_query = query.copy()
        pit_query["pit"] = {"id": pit_id, "keep_alive": keep_alive}
        # Add _shard_doc tiebreaker for efficient pagination with PIT
        pit_query["sort"] = query.get("sort", [{"@timestamp": "asc"}]) + [
            {"_shard_doc": "asc"}
        ]

        # Open file for streaming writes (JSONL only)
        output_file = None
        if output and output_format == "jsonl":
            output_file = open(output, "w")  # noqa: SIM115

        console.print("Fetching all pages...")
        try:
            with Progress(
                SpinnerColumn(),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn("•"),
                TextColumn("[progress.description]{task.description}"),
                TextColumn("•"),
                TimeElapsedColumn(),
                TextColumn("eta"),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(
                    "Starting...", total=total_docs if total_docs > 0 else None
                )

                while True:
                    page += 1
                    if search_after:
                        pit_query["search_after"] = search_after

                    search_result = client.search_with_pit(pit_query)
                    hits = search_result.hit_list

                    if not hits:
                        break

                    # Stream write for JSONL format
                    if output_file:
                        for hit in hits:
                            output_file.write(json.dumps(hit) + "\n")
                        output_file.flush()
                        docs_written += len(hits)
                    else:
                        all_hits.extend(hits)

                    current_count = docs_written if output_file else len(all_hits)
                    progress.update(
                        task,
                        completed=current_count,
                        description=f"Page {page} • {current_count:,} docs",
                    )

                    search_after = hits[-1].get("sort")

                    # Refresh PIT keep-alive
                    if search_result.pit_id:
                        pit_query["pit"]["id"] = search_result.pit_id
        finally:
            # Step 5: Close PIT and output file
            client.close_pit(pit_id)
            if output_file:
                output_file.close()

    final_count = docs_written if docs_written > 0 else len(all_hits)
    console.print(f"[green]Export complete! Total documents: {final_count:,}[/green]")

    # Write output (only if not already streamed)
    if not (output and output_format == "jsonl"):
        formatted = format_hits(all_hits, output_format)
        write_output(
            formatted,
            output,
            console,
            success_message=f"Wrote to {output}" if output else None,
        )
    elif output:
        console.print(f"Wrote to {output}")


@search.command(name="import")
@click.option(
    "--index",
    required=True,
    help="Destination index to import into",
)
@click.option(
    "--input",
    "input_file",
    "-i",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to JSONL export file",
)
@click.option(
    "--batch-size",
    default=1000,
    type=int,
    help="Documents per bulk request (default: 1000)",
)
@click.option(
    "--refresh",
    type=click.Choice(["false", "wait_for", "true"]),
    default="false",
    help="Refresh behavior for bulk requests (default: false)",
)
@click.option(
    "--url",
    required=True,
    help="Destination Elasticsearch URL",
)
@click.option("--api-key-id", help="Destination API key ID")
@click.option("--api-key", help="Destination API key value")
@click.option("--username", help="Destination username (basic auth)")
@click.option("--password", help="Destination password (basic auth)")
def import_docs(
    index: str,
    input_file: Path,
    batch_size: int,
    refresh: str,
    url: str,
    api_key_id: str | None,
    api_key: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Import JSONL hits into Elasticsearch using bulk create operations."""
    if batch_size <= 0:
        console.print("[red]--batch-size must be greater than 0.[/red]")
        raise SystemExit(1)

    client = create_client(
        url=url,
        api_key_id=api_key_id,
        api_key=api_key,
        username=username,
        password=password,
    )

    total_read = 0
    created = 0
    conflicts = 0
    failed = 0
    batch_lines: list[str] = []
    batch_docs = 0

    def flush_batch() -> None:
        nonlocal created, conflicts, failed, batch_docs
        if not batch_lines:
            return

        response = client.bulk("\n".join(batch_lines) + "\n", refresh=refresh)
        for item in response.get("items", []):
            create_result = item.get("create", {})
            status = create_result.get("status", 0)
            if 200 <= status < 300:
                created += 1
            elif status == 409:
                conflicts += 1
            else:
                failed += 1
                error = create_result.get("error")
                if isinstance(error, dict):
                    error_type = error.get("type", "unknown")
                    reason = error.get("reason", "unknown error")
                    console.print(
                        "[red]Bulk item failed[/red]"
                        f" (_id={create_result.get('_id', '?')}, status={status}): "
                        f"{error_type}: {reason}"
                    )
                else:
                    console.print(
                        "[red]Bulk item failed[/red]"
                        f" (_id={create_result.get('_id', '?')}, status={status})"
                    )

        batch_lines.clear()
        batch_docs = 0

    with client.session():
        if not client.index_exists(index):
            console.print(
                f"[red]Destination index [bold]{index}[/bold] does not exist.[/red]"
            )
            raise SystemExit(1)

        console.print(f"[bold]Starting import into {index}[/bold]")
        with input_file.open() as f:
            for line_number, line in enumerate(f, start=1):
                if not line.strip():
                    continue

                total_read += 1
                try:
                    hit = json.loads(line)
                except json.JSONDecodeError as e:
                    console.print(
                        f"[red]Invalid JSON at line {line_number}:[/red] {e.msg}"
                    )
                    raise SystemExit(1)

                if not isinstance(hit, dict):
                    console.print(
                        f"[red]Line {line_number} must be a JSON object.[/red]"
                    )
                    raise SystemExit(1)

                doc_id = hit.get("_id")
                source = hit.get("_source")
                if not isinstance(doc_id, str):
                    console.print(
                        f"[red]Line {line_number} missing string '_id' field.[/red]"
                    )
                    raise SystemExit(1)
                if not isinstance(source, dict):
                    console.print(
                        f"[red]Line {line_number} missing object '_source' field.[/red]"
                    )
                    raise SystemExit(1)

                batch_lines.append(
                    json.dumps({"create": {"_index": index, "_id": doc_id}})
                )
                batch_lines.append(json.dumps(source))
                batch_docs += 1

                if batch_docs >= batch_size:
                    flush_batch()
                    console.print(
                        f"Processed {total_read:,} docs "
                        f"(created: {created:,}, conflicts: {conflicts:,}, failed: {failed:,})"
                    )

        flush_batch()

    console.print(
        "[green]Import complete![/green] "
        f"Read: {total_read:,}, Created: {created:,}, "
        f"Conflicts skipped: {conflicts:,}, Failed: {failed:,}"
    )
    if failed > 0:
        raise SystemExit(1)

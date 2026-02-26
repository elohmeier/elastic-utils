"""Search commands for Elasticsearch async search and export."""

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

import click
import httpx
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


def print_shard_failures(
    failures: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> None:
    """Print shard failure diagnostics in a readable format."""
    shown = failures if limit is None else failures[:limit]
    for idx, failure in enumerate(shown, start=1):
        index = failure.get("index", "?")
        shard = failure.get("shard", "?")
        node = failure.get("node", "?")
        reason = failure.get("reason")
        if isinstance(reason, dict):
            reason_type = reason.get("type", "unknown")
            reason_msg = reason.get("reason", "unknown")
        else:
            reason_type = "unknown"
            reason_msg = str(reason)

        console.print(
            f"[yellow]  shard failure {idx}:[/yellow] "
            f"index={index} shard={shard} node={node}"
        )
        console.print(f"[yellow]    reason:[/yellow] {reason_type}: {reason_msg}")

    if limit is not None and len(failures) > limit:
        console.print(
            f"[yellow]  ... plus {len(failures) - limit} more shard failures[/yellow]"
        )


def _format_bytes(value: Any) -> str:
    """Format byte counts for compact diagnostics."""
    if not isinstance(value, (int, float)) or value < 0:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f}{units[idx]}"


def print_failed_shard_allocation_debug(
    client: ElasticsearchClient, failures: list[dict[str, Any]]
) -> None:
    """Show current shard routing state for failed shard entries."""
    failed_targets: dict[str, set[str]] = {}
    for failure in failures:
        index = failure.get("index")
        shard = failure.get("shard")
        if not isinstance(index, str) or shard is None:
            continue
        failed_targets.setdefault(index, set()).add(str(shard))

    if not failed_targets:
        console.print(
            "[yellow]Could not infer index/shard targets from failure details.[/yellow]"
        )
        return

    console.print("[bold]Failed Shard Routing[/bold]")
    for index, failed_shards in sorted(failed_targets.items()):
        try:
            response = client.get(
                f"/_cat/shards/{index}",
                params={
                    "format": "json",
                    "h": "index,shard,prirep,state,node,unassigned.reason",
                },
            )
        except SystemExit:
            console.print(
                f"[yellow]  Unable to fetch shard routing for index {index}.[/yellow]"
            )
            continue
        if response is None:
            continue

        rows = response.json()
        matching_rows = [
            row
            for row in rows
            if isinstance(row, dict) and row.get("shard") in failed_shards
        ]
        if not matching_rows:
            console.print(
                f"[yellow]  No matching shard rows found for index={index}.[/yellow]"
            )
            continue

        for row in matching_rows:
            shard = row.get("shard", "?")
            prirep = row.get("prirep", "?")
            state = row.get("state", "?")
            node = row.get("node", "?")
            unassigned = row.get("unassigned.reason") or "-"
            console.print(
                f"  index={index} shard={shard}{prirep} "
                f"state={state} node={node} unassigned={unassigned}"
            )


def print_node_diagnostics(
    client: ElasticsearchClient, failures: list[dict[str, Any]]
) -> None:
    """Show key node stats for nodes implicated in shard failures."""
    nodes = sorted(
        {
            node
            for node in (failure.get("node") for failure in failures)
            if isinstance(node, str) and node
        }
    )
    if not nodes:
        console.print("[yellow]No node IDs present in shard failure details.[/yellow]")
        return

    console.print("[bold]Impacted Node Stats[/bold]")
    for node_id in nodes:
        try:
            response = client.get(f"/_nodes/{node_id}/stats/fs,indices,thread_pool")
        except SystemExit:
            console.print(
                f"[yellow]  Unable to fetch node stats for node={node_id}.[/yellow]"
            )
            continue
        if response is None:
            continue

        payload = response.json()
        nodes_payload = payload.get("nodes", {})
        if not isinstance(nodes_payload, dict) or not nodes_payload:
            console.print(f"[yellow]  No node payload for node={node_id}.[/yellow]")
            continue

        node = next(iter(nodes_payload.values()))
        if not isinstance(node, dict):
            console.print(
                f"[yellow]  Unexpected node payload format for {node_id}.[/yellow]"
            )
            continue

        node_name = node.get("name", node_id)
        fs_total = node.get("fs", {}).get("total", {})
        indices_search = node.get("indices", {}).get("search", {})
        tp_search = node.get("thread_pool", {}).get("search", {})
        console.print(
            "  "
            f"node={node_name} ({node_id}) "
            f"disk_avail={_format_bytes(fs_total.get('available_in_bytes'))} "
            f"disk_total={_format_bytes(fs_total.get('total_in_bytes'))} "
            f"search_q={tp_search.get('queue', '?')} "
            f"search_rejected={tp_search.get('rejected', '?')} "
            f"query_total={indices_search.get('query_total', '?')}"
        )


def should_cancel_async_search_on_interrupt() -> bool:
    """Decide whether to cancel a preflight async search after Ctrl-C."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return True
    return click.confirm(
        "Cancel async search on Elasticsearch before exiting?",
        default=True,
    )


@dataclass
class ExportChunk:
    """Batch of hits produced by a slice worker."""

    hits: list[dict[str, Any]]
    page_size: int
    page_duration: float
    payload_bytes: int


def adapt_page_size(
    current_size: int,
    page_duration: float,
    payload_bytes: int,
    returned_hits: int,
    *,
    min_page_size: int,
    max_page_size: int,
) -> int:
    """Adapt page size based on latency and response payload."""
    if returned_hits == 0 or returned_hits < current_size:
        return current_size

    next_size = current_size
    if page_duration < 0.8 and payload_bytes < 8_000_000:
        next_size = int(current_size * 1.25)
    elif page_duration > 2.5 or payload_bytes > 20_000_000:
        next_size = int(current_size * 0.7)

    return max(min_page_size, min(max_page_size, next_size))


class HitSink:
    """Streaming sink for exported hits."""

    def write_hits(self, hits: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class JsonlSink(HitSink):
    """Writes hits in JSONL format."""

    def __init__(self, output: Path | None) -> None:
        self._output = output
        self._file = output.open("w") if output else None

    def write_hits(self, hits: list[dict[str, Any]]) -> None:
        if self._file:
            for hit in hits:
                self._file.write(json.dumps(hit) + "\n")
            self._file.flush()
            return
        for hit in hits:
            print(json.dumps(hit))

    def close(self) -> None:
        if self._file:
            self._file.close()


class ParquetSink(HitSink):
    """Writes hits to a parquet stream with buffered row groups."""

    def __init__(self, output: Path, compression: str, row_group_size: int) -> None:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            console.print(
                "[red]Parquet support requires pyarrow. Install with:[/red] "
                "[bold]uv sync[/bold]"
            )
            raise SystemExit(1)

        codec = None if compression == "none" else compression
        self._pa = pa
        self._schema = pa.schema(
            [
                pa.field("hit_json", pa.large_string()),
                pa.field("_id", pa.string()),
                pa.field("_index", pa.string()),
            ]
        )
        self._writer = pq.ParquetWriter(
            str(output),
            self._schema,
            compression=codec,
        )
        self._row_group_size = row_group_size
        self._rows: list[dict[str, str | None]] = []

    def _flush_rows(self) -> None:
        if not self._rows:
            return

        table = self._pa.table(
            {
                "hit_json": self._pa.array(
                    [row["hit_json"] for row in self._rows],
                    type=self._pa.large_string(),
                ),
                "_id": self._pa.array(
                    [row["_id"] for row in self._rows], type=self._pa.string()
                ),
                "_index": self._pa.array(
                    [row["_index"] for row in self._rows], type=self._pa.string()
                ),
            },
            schema=self._schema,
        )
        self._writer.write_table(table)
        self._rows.clear()

    def write_hits(self, hits: list[dict[str, Any]]) -> None:
        for hit in hits:
            self._rows.append(
                {
                    "hit_json": json.dumps(hit, separators=(",", ":")),
                    "_id": hit.get("_id"),
                    "_index": hit.get("_index"),
                }
            )
            if len(self._rows) >= self._row_group_size:
                self._flush_rows()

    def close(self) -> None:
        self._flush_rows()
        self._writer.close()


def infer_input_format(input_file: Path, input_format: str | None) -> str:
    """Infer import format from extension unless explicitly set."""
    if input_format:
        return input_format
    if input_file.suffix.lower() == ".parquet":
        return "parquet"
    return "jsonl"


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


@search.command(name="debug-shards")
@click.argument("search_id")
@click.option(
    "--wait-for",
    default=None,
    help="Optional wait timeout before fetching status (e.g., 5s)",
)
@click.option(
    "--deep/--no-deep",
    default=False,
    help=("Fetch extra diagnostics (failed shard routing and impacted node stats)."),
)
def debug_shards(search_id: str, wait_for: str | None, deep: bool) -> None:
    """Show shard failure diagnostics for an async search ID."""
    client = ElasticsearchClient.from_credentials(console)
    result = client.async_search_status(search_id, wait_for=wait_for)

    status_color = "yellow" if result.is_running else "green"
    console.print(
        f"[{status_color}]Status: {'Running' if result.is_running else 'Complete'}[/{status_color}]"
    )
    console.print(f"  Partial: {result.is_partial}")
    console.print(f"  Shards: {format_shards(result.response.shards)}")
    console.print(f"  Took: {result.response.took}ms")

    failures = result.response.shards.failures
    failed = result.response.shards.failed
    if failed <= 0:
        console.print("[green]No failed shards reported.[/green]")
        return

    console.print(f"[yellow]Found {failed} failed shard(s).[/yellow]")
    if not failures:
        console.print(
            "[yellow]No detailed failure payload returned by Elasticsearch.[/yellow]"
        )
        return
    print_shard_failures(failures)
    if deep:
        print_failed_shard_allocation_debug(client, failures)
        print_node_diagnostics(client, failures)


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
@click.option(
    "--wait-for",
    default="5s",
    help="Wait timeout for completion before fetching results (default: 5s)",
)
def get(search_id: str, output: Path | None, output_format: str, wait_for: str) -> None:
    """Get the results of an async search."""
    client = ElasticsearchClient.from_credentials(console)

    result = client.async_search_status(search_id, wait_for=wait_for)

    hits = result.hits
    if not hits and result.total_hits > 0:
        if result.is_running:
            console.print(
                "[yellow]Search has matching documents but no hits are available "
                "in this partial response yet.[/yellow]"
            )
            console.print(
                "Try [bold]elastic-utils search wait[/bold] first, "
                "then [bold]search get[/bold] again."
            )
        else:
            console.print(
                "[yellow]Search matched documents but returned zero hit "
                "documents.[/yellow]"
            )
            console.print(
                "This usually means the original query used "
                '[bold]"size": 0[/bold]. Re-submit with [bold]"size" > 0[/bold].'
            )
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
    type=click.Choice(["json", "jsonl", "parquet"]),
    default="jsonl",
    help="Output format (default: jsonl)",
)
@click.option(
    "--page-size",
    default=1000,
    type=int,
    help="Initial results per page (default: 1000)",
)
@click.option(
    "--workers",
    type=int,
    default=0,
    help="Number of parallel PIT slice workers (default: auto by shards, max 8)",
)
@click.option(
    "--adaptive-page-size/--no-adaptive-page-size",
    default=True,
    help="Auto-tune page size by latency/payload (default: enabled)",
)
@click.option(
    "--min-page-size",
    default=250,
    type=int,
    help="Minimum adaptive page size (default: 250)",
)
@click.option(
    "--max-page-size",
    default=5000,
    type=int,
    help="Maximum adaptive page size (default: 5000)",
)
@click.option(
    "--parquet-compression",
    type=click.Choice(["zstd", "snappy", "gzip", "none"]),
    default="zstd",
    help="Parquet compression codec (default: zstd)",
)
@click.option(
    "--parquet-row-group-size",
    default=50000,
    type=int,
    help="Rows per parquet row group (default: 50000)",
)
@click.option(
    "--keep-alive",
    default="10m",
    help="PIT keep-alive duration (default: 10m)",
)
@click.option(
    "--request-timeout",
    default=120.0,
    type=float,
    help="Per PIT search request timeout in seconds (default: 120)",
)
@click.option(
    "--max-timeout-retries",
    default=5,
    type=int,
    help="Max retries per PIT page on read timeout (default: 5)",
)
@click.option(
    "--fail-on-shard-failures/--allow-shard-failures",
    default=True,
    help=(
        "Fail export when preflight async search reports failed shards (default: fail)"
    ),
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
    workers: int,
    adaptive_page_size: bool,
    min_page_size: int,
    max_page_size: int,
    parquet_compression: str,
    parquet_row_group_size: int,
    keep_alive: str,
    request_timeout: float,
    max_timeout_retries: int,
    fail_on_shard_failures: bool,
    from_date: str | None,
    to_date: str | None,
    url: str | None,
    api_key_id: str | None,
    api_key: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Export all search results using async search + PIT pagination."""
    if page_size <= 0:
        console.print("[red]--page-size must be greater than 0.[/red]")
        raise SystemExit(1)
    if min_page_size <= 0 or max_page_size <= 0:
        console.print("[red]--min-page-size and --max-page-size must be > 0.[/red]")
        raise SystemExit(1)
    if min_page_size > max_page_size:
        console.print(
            "[red]--min-page-size cannot be greater than --max-page-size.[/red]"
        )
        raise SystemExit(1)
    if workers < 0:
        console.print("[red]--workers cannot be negative.[/red]")
        raise SystemExit(1)
    if parquet_row_group_size <= 0:
        console.print("[red]--parquet-row-group-size must be greater than 0.[/red]")
        raise SystemExit(1)
    if output_format == "parquet" and not output:
        console.print("[red]Parquet export requires --output.[/red]")
        raise SystemExit(1)
    if request_timeout <= 0:
        console.print("[red]--request-timeout must be greater than 0.[/red]")
        raise SystemExit(1)
    if max_timeout_retries < 0:
        console.print("[red]--max-timeout-retries cannot be negative.[/red]")
        raise SystemExit(1)

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
    with client.session():
        result = None
        console.print("Running initial async search...")
        initial_result = client.async_search_submit(
            index, query, wait_for="1s", keep_alive="1h"
        )
        async_search_id = initial_result.id
        if not async_search_id:
            console.print("[red]Async search did not return a search ID.[/red]")
            raise SystemExit(1)
        console.print(f"Async search ID: [bold]{async_search_id}[/bold]")

        console.print("Waiting for async search to complete...")
        try:
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
                        description=(
                            f"(skipped: {shards.skipped}, failed: {shards.failed})"
                        ),
                    )
                    if not result.is_running:
                        break
                    time.sleep(5)
        except KeyboardInterrupt:
            if should_cancel_async_search_on_interrupt():
                client.async_search_delete(async_search_id, silent=True)
                console.print(
                    "\n[yellow]Interrupt received, canceled async search.[/yellow]"
                )
            else:
                console.print(
                    "\n[yellow]Interrupt received, leaving async search running "
                    "(will expire by keep_alive).[/yellow]"
                )
            raise SystemExit(130)

        total_docs = result.total_hits if result else 0
        console.print(f"Initial search complete, total matching docs: {total_docs:,}")
        if result and result.response.shards.failed > 0:
            failed = result.response.shards.failed
            console.print(
                f"[yellow]Warning:[/yellow] preflight had {failed} failed shards."
            )
            failures = result.response.shards.failures
            if not failures:
                console.print(
                    "[yellow]No detailed failure payload returned by Elasticsearch.[/yellow]"
                )
            else:
                print_shard_failures(failures, limit=3)
            if fail_on_shard_failures:
                client.async_search_delete(async_search_id, silent=True)
                console.print(
                    "[red]Export failed:[/red] preflight shard failures detected. "
                    "Re-run with [bold]--allow-shard-failures[/bold] to continue."
                )
                raise SystemExit(1)
        client.async_search_delete(async_search_id, silent=True)

        shard_count = client.primary_shard_count(index)
        resolved_workers = workers if workers > 0 else max(1, min(shard_count, 8))
        if output_format == "json":
            resolved_workers = 1
        console.print(
            f"Fetching with {resolved_workers} worker(s)"
            f"{' and adaptive page sizing' if adaptive_page_size else ''}..."
        )

    if output_format == "json":
        all_hits: list[dict[str, Any]] = []
        sink: HitSink | None = None
    elif output_format == "jsonl":
        sink = JsonlSink(output)
        all_hits = []
    else:
        assert output is not None
        sink = ParquetSink(output, parquet_compression, parquet_row_group_size)
        all_hits = []

    queue_max_size = max(2 * resolved_workers, 2)
    chunks: Queue[ExportChunk | Exception | None] = Queue(maxsize=queue_max_size)
    stop_event = threading.Event()
    worker_errors: list[str] = []
    lock = threading.Lock()
    aborted = False

    def enqueue_chunk(item: ExportChunk | Exception | None) -> None:
        """Put items into the queue without blocking shutdown forever."""
        while not stop_event.is_set():
            try:
                chunks.put(item, timeout=0.2)
                return
            except Full:
                continue
        try:
            chunks.put_nowait(item)
        except Full:
            pass

    def worker(worker_id: int) -> None:
        local_client = create_client(
            url=url,
            api_key_id=api_key_id,
            api_key=api_key,
            username=username,
            password=password,
        )
        pit_id: str | None = None
        try:
            with local_client.session():
                pit_id = local_client.open_pit(index, keep_alive=keep_alive)
                local_page_size = max(min(page_size, max_page_size), min_page_size)
                search_after = None
                while not stop_event.is_set():
                    pit_query = query.copy()
                    pit_query["size"] = local_page_size
                    pit_query["pit"] = {"id": pit_id, "keep_alive": keep_alive}
                    if resolved_workers > 1:
                        pit_query["slice"] = {"id": worker_id, "max": resolved_workers}
                    pit_query["sort"] = query.get("sort", [{"@timestamp": "asc"}]) + [
                        {"_shard_doc": "asc"}
                    ]
                    if search_after:
                        pit_query["search_after"] = search_after

                    response: dict[str, Any] | None = None
                    page_duration = 0.0
                    timeout_failures = 0
                    while timeout_failures <= max_timeout_retries:
                        try:
                            page_start = time.perf_counter()
                            response = local_client.search_with_pit_raw(
                                pit_query,
                                timeout=request_timeout,
                            )
                            page_duration = time.perf_counter() - page_start
                            break
                        except httpx.ReadTimeout:
                            timeout_failures += 1
                            if timeout_failures > max_timeout_retries:
                                raise RuntimeError(
                                    "Read timeout while fetching page "
                                    f"(worker={worker_id}, page_size={local_page_size}, "
                                    f"retries={max_timeout_retries})."
                                ) from None
                            reduced_page_size = max(
                                min_page_size,
                                int(local_page_size * 0.5),
                            )
                            if reduced_page_size < local_page_size:
                                local_page_size = reduced_page_size
                            backoff = min(2**timeout_failures, 10)
                            time.sleep(backoff)

                    if response is None:
                        break

                    hits_root = response.get("hits", {})
                    hits = hits_root.get("hits", [])
                    if not isinstance(hits, list):
                        raise ValueError(
                            "Unexpected search response: hits.hits is not a list"
                        )
                    if not hits:
                        break

                    payload_bytes = len(json.dumps(response, separators=(",", ":")))
                    enqueue_chunk(
                        ExportChunk(
                            hits=hits,
                            page_size=local_page_size,
                            page_duration=page_duration,
                            payload_bytes=payload_bytes,
                        )
                    )

                    search_after = hits[-1].get("sort")
                    next_pit_id = response.get("pit_id")
                    if isinstance(next_pit_id, str) and next_pit_id:
                        pit_id = next_pit_id

                    if adaptive_page_size:
                        local_page_size = adapt_page_size(
                            local_page_size,
                            page_duration,
                            payload_bytes,
                            len(hits),
                            min_page_size=min_page_size,
                            max_page_size=max_page_size,
                        )
        except Exception as e:
            enqueue_chunk(e)
            stop_event.set()
        finally:
            if pit_id:
                try:
                    local_client.close_pit(pit_id)
                except Exception:
                    pass
            enqueue_chunk(None)

    docs_written = 0
    pages = 0
    recent_page_size = page_size

    executor = ThreadPoolExecutor(max_workers=resolved_workers)
    futures = [
        executor.submit(worker, worker_id) for worker_id in range(resolved_workers)
    ]
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
            finished_workers = 0
            while finished_workers < resolved_workers:
                try:
                    chunk = chunks.get(timeout=0.2)
                except Empty:
                    continue
                if chunk is None:
                    finished_workers += 1
                    continue

                if isinstance(chunk, Exception):
                    with lock:
                        worker_errors.append(str(chunk))
                    stop_event.set()
                    continue

                pages += 1
                if sink:
                    sink.write_hits(chunk.hits)
                else:
                    all_hits.extend(chunk.hits)

                docs_written += len(chunk.hits)
                recent_page_size = chunk.page_size
                progress.update(
                    task,
                    completed=docs_written,
                    description=(
                        f"Pages {pages} • {docs_written:,} docs"
                        f" • page_size~{recent_page_size}"
                    ),
                )

        for future in futures:
            future.result()
    except KeyboardInterrupt:
        aborted = True
        stop_event.set()
        console.print("\n[yellow]Interrupt received, stopping workers...[/yellow]")
    finally:
        stop_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        if sink:
            sink.close()

    if aborted:
        raise SystemExit(130)

    if worker_errors:
        console.print(f"[red]Export failed:[/red] {worker_errors[0]}")
        raise SystemExit(1)

    final_count = docs_written if docs_written > 0 else len(all_hits)
    console.print(f"[green]Export complete! Total documents: {final_count:,}[/green]")

    if output_format == "json":
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
    help="Path to export file (jsonl or parquet)",
)
@click.option(
    "--input-format",
    type=click.Choice(["jsonl", "parquet"]),
    default=None,
    help="Input format (default: inferred from extension)",
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
    input_format: str | None,
    batch_size: int,
    refresh: str,
    url: str,
    api_key_id: str | None,
    api_key: str | None,
    username: str | None,
    password: str | None,
) -> None:
    """Import JSONL or Parquet hits into Elasticsearch using bulk create operations."""
    if batch_size <= 0:
        console.print("[red]--batch-size must be greater than 0.[/red]")
        raise SystemExit(1)
    resolved_input_format = infer_input_format(input_file, input_format)

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

    def process_hit(hit: Any, line_number: int) -> None:
        nonlocal total_read, batch_docs
        if not isinstance(hit, dict):
            console.print(f"[red]Line {line_number} must be a JSON object.[/red]")
            raise SystemExit(1)

        total_read += 1
        doc_id = hit.get("_id")
        source = hit.get("_source")
        if not isinstance(doc_id, str):
            console.print(f"[red]Line {line_number} missing string '_id' field.[/red]")
            raise SystemExit(1)
        if not isinstance(source, dict):
            console.print(
                f"[red]Line {line_number} missing object '_source' field.[/red]"
            )
            raise SystemExit(1)

        batch_lines.append(json.dumps({"create": {"_index": index, "_id": doc_id}}))
        batch_lines.append(json.dumps(source))
        batch_docs += 1

        if batch_docs >= batch_size:
            flush_batch()
            console.print(
                f"Processed {total_read:,} docs "
                f"(created: {created:,}, conflicts: {conflicts:,}, failed: {failed:,})"
            )

    with client.session():
        if not client.index_exists(index):
            console.print(
                f"[red]Destination index [bold]{index}[/bold] does not exist.[/red]"
            )
            raise SystemExit(1)

        console.print(f"[bold]Starting import into {index}[/bold]")
        if resolved_input_format == "jsonl":
            with input_file.open() as f:
                for line_number, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    try:
                        hit = json.loads(line)
                    except json.JSONDecodeError as e:
                        console.print(
                            f"[red]Invalid JSON at line {line_number}:[/red] {e.msg}"
                        )
                        raise SystemExit(1)
                    process_hit(hit, line_number)
        else:
            try:
                import pyarrow.parquet as pq
            except ImportError:
                console.print(
                    "[red]Parquet support requires pyarrow. Install with:[/red] "
                    "[bold]uv sync[/bold]"
                )
                raise SystemExit(1)

            parquet_file = pq.ParquetFile(str(input_file))
            line_number = 0
            for batch in parquet_file.iter_batches():
                batch_rows = batch.to_pylist()
                for row in batch_rows:
                    line_number += 1
                    if not isinstance(row, dict):
                        console.print("[red]Invalid parquet row format.[/red]")
                        raise SystemExit(1)

                    hit_json = row.get("hit_json")
                    if not isinstance(hit_json, str):
                        console.print(
                            (
                                f"[red]Parquet row {line_number} missing string "
                                "'hit_json'.[/red]"
                            )
                        )
                        raise SystemExit(1)
                    try:
                        hit = json.loads(hit_json)
                    except json.JSONDecodeError as e:
                        console.print(
                            (
                                f"[red]Invalid hit_json at parquet row "
                                f"{line_number}:[/red] {e.msg}"
                            )
                        )
                        raise SystemExit(1)
                    process_hit(hit, line_number)

        flush_batch()

    console.print(
        "[green]Import complete![/green] "
        f"Read: {total_read:,}, Created: {created:,}, "
        f"Conflicts skipped: {conflicts:,}, Failed: {failed:,}"
    )
    if failed > 0:
        raise SystemExit(1)

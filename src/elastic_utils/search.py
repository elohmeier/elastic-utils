"""Search commands for Elasticsearch async search and export."""

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import load_credentials

console = Console()


def get_auth() -> tuple[str, dict[str, str]]:
    """Get authentication credentials. Returns (url, headers dict with Authorization)."""
    creds = load_credentials()
    if creds is None:
        console.print("[yellow]Not authenticated.[/yellow]")
        console.print("Run [bold]elastic-utils auth login[/bold] to authenticate.")
        raise SystemExit(1)

    # Elasticsearch API key auth requires base64 encoding of "id:api_key"
    api_key_id = creds["api_key_id"]
    api_key = creds["api_key"]
    encoded = base64.b64encode(f"{api_key_id}:{api_key}".encode()).decode()
    headers = {"Authorization": f"ApiKey {encoded}"}

    return creds["url"], headers


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


def format_shards(shards: dict[str, Any]) -> str:
    """Format shard progress info."""
    total = shards.get("total", 0)
    successful = shards.get("successful", 0)
    skipped = shards.get("skipped", 0)
    failed = shards.get("failed", 0)
    return f"{successful}/{total} (skipped: {skipped}, failed: {failed})"


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
    url, headers = get_auth()
    query = read_query(query_file)

    console.print(f"Submitting async search to [bold]{index}[/bold]...")

    try:
        response = httpx.post(
            f"{url}/{index}/_async_search",
            params={
                "wait_for_completion_timeout": wait_for,
                "keep_on_completion": "true",
                "keep_alive": keep_alive,
            },
            headers=headers,
            json=query,
            timeout=60.0,
        )
        response.raise_for_status()
    except httpx.ConnectError as e:
        console.print(f"[red]Connection error:[/red] {e}")
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        console.print(
            f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
        )
        raise SystemExit(1)

    data = response.json()
    search_id = data.get("id")
    is_running = data.get("is_running", False)
    is_partial = data.get("is_partial", False)
    shards = data.get("response", {}).get("_shards", {})

    console.print("[green]Search submitted![/green]")
    console.print(f"  Search ID: [bold]{search_id}[/bold]")
    console.print(f"  Running: {is_running}")
    console.print(f"  Partial: {is_partial}")
    console.print(f"  Shards: {format_shards(shards)}")


@search.command()
@click.argument("search_id")
@click.option(
    "--wait-for",
    default=None,
    help="Wait timeout for completion (e.g., 5s)",
)
def status(search_id: str, wait_for: str | None) -> None:
    """Check the status of an async search."""
    url, headers = get_auth()

    params = {}
    if wait_for:
        params["wait_for_completion_timeout"] = wait_for

    try:
        response = httpx.get(
            f"{url}/_async_search/{search_id}",
            params=params,
            headers=headers,
            timeout=120.0,
        )
        response.raise_for_status()
    except httpx.ConnectError as e:
        console.print(f"[red]Connection error:[/red] {e}")
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print("[red]Search not found.[/red]")
        else:
            console.print(
                f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
            )
        raise SystemExit(1)

    data = response.json()
    is_running = data.get("is_running", False)
    is_partial = data.get("is_partial", False)
    shards = data.get("response", {}).get("_shards", {})
    took = data.get("response", {}).get("took", 0)
    hits_total = len(data.get("response", {}).get("hits", {}).get("hits", []))

    status_color = "yellow" if is_running else "green"
    console.print(
        f"[{status_color}]Status: {'Running' if is_running else 'Complete'}[/{status_color}]"
    )
    console.print(f"  Partial: {is_partial}")
    console.print(f"  Shards: {format_shards(shards)}")
    console.print(f"  Took: {took}ms")
    console.print(f"  Hits returned: {hits_total}")


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
    url, headers = get_auth()

    start_time = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Waiting for search to complete...", total=None)

        while True:
            try:
                response = httpx.get(
                    f"{url}/_async_search/{search_id}",
                    headers=headers,
                    timeout=30.0,
                )
                response.raise_for_status()
            except httpx.ConnectError as e:
                console.print(f"[red]Connection error:[/red] {e}")
                raise SystemExit(1)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    console.print("[red]Search not found.[/red]")
                else:
                    console.print(
                        f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
                    )
                raise SystemExit(1)

            data = response.json()
            is_running = data.get("is_running", False)
            shards = data.get("response", {}).get("_shards", {})

            progress.update(
                task,
                description=f"Shards: {format_shards(shards)} | Elapsed: {int(time.time() - start_time)}s",
            )

            if not is_running:
                break

            if timeout and (time.time() - start_time) >= timeout:
                console.print("[yellow]Timeout reached, search still running.[/yellow]")
                raise SystemExit(1)

            time.sleep(interval)

    # Final status
    took = data.get("response", {}).get("took", 0)
    hits_total = len(data.get("response", {}).get("hits", {}).get("hits", []))

    console.print("[green]Search complete![/green]")
    console.print(f"  Shards: {format_shards(shards)}")
    console.print(f"  Took: {took}ms")
    console.print(f"  Hits returned: {hits_total}")


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
    url, headers = get_auth()

    try:
        response = httpx.get(
            f"{url}/_async_search/{search_id}",
            headers=headers,
            timeout=120.0,
        )
        response.raise_for_status()
    except httpx.ConnectError as e:
        console.print(f"[red]Connection error:[/red] {e}")
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print("[red]Search not found.[/red]")
        else:
            console.print(
                f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
            )
        raise SystemExit(1)

    data = response.json()
    hits = data.get("response", {}).get("hits", {}).get("hits", [])

    if output_format == "json":
        result = json.dumps(hits, indent=2)
    else:  # jsonl
        result = "\n".join(json.dumps(hit) for hit in hits)

    if output:
        output.write_text(result)
        console.print(f"[green]Wrote {len(hits)} hits to {output}[/green]")
    else:
        print(result)


@search.command()
@click.argument("search_id")
def delete(search_id: str) -> None:
    """Delete an async search."""
    url, headers = get_auth()

    try:
        response = httpx.delete(
            f"{url}/_async_search/{search_id}",
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.ConnectError as e:
        console.print(f"[red]Connection error:[/red] {e}")
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(
                "[yellow]Search not found (may have already expired).[/yellow]"
            )
            return
        console.print(
            f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
        )
        raise SystemExit(1)

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
    url, headers = get_auth()
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

    # Step 1: Run async search to verify query works on frozen indices
    console.print("Running initial async search...")
    try:
        response = httpx.post(
            f"{url}/{index}/_async_search",
            params={
                "wait_for_completion_timeout": "1s",
                "keep_on_completion": "true",
                "keep_alive": "1h",
            },
            headers=headers,
            json=query,
            timeout=60.0,
        )
        response.raise_for_status()
    except httpx.ConnectError as e:
        console.print(f"[red]Connection error:[/red] {e}")
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        console.print(
            f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
        )
        raise SystemExit(1)

    data = response.json()
    async_search_id = data.get("id")

    # Step 2: Wait for async search to complete
    console.print("Waiting for async search to complete...")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Processing...", total=None)

        while True:
            try:
                response = httpx.get(
                    f"{url}/_async_search/{async_search_id}",
                    headers=headers,
                    timeout=30.0,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                console.print(
                    f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
                )
                raise SystemExit(1)

            data = response.json()
            is_running = data.get("is_running", False)
            shards = data.get("response", {}).get("_shards", {})

            progress.update(task, description=f"Shards: {format_shards(shards)}")

            if not is_running:
                break

            time.sleep(5)

    initial_hits = data.get("response", {}).get("hits", {}).get("hits", [])
    console.print(
        f"Initial search complete, got {len(initial_hits)} hits in first page"
    )

    # Cleanup async search
    try:
        httpx.delete(
            f"{url}/_async_search/{async_search_id}",
            headers=headers,
            timeout=30.0,
        )
    except httpx.HTTPStatusError:
        pass  # Ignore cleanup errors

    # Step 3: Open PIT for pagination
    console.print("Opening Point-in-Time for pagination...")
    try:
        response = httpx.post(
            f"{url}/{index}/_pit",
            params={"keep_alive": keep_alive},
            headers=headers,
            timeout=60.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        console.print(
            f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
        )
        raise SystemExit(1)

    pit_id = response.json().get("id")

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

            try:
                response = httpx.post(
                    f"{url}/_search",
                    headers=headers,
                    json=pit_query,
                    timeout=120.0,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                console.print(
                    f"[red]HTTP error {e.response.status_code}:[/red] {escape(e.response.text)}"
                )
                # Cleanup PIT on error
                try:
                    httpx.request(
                        "DELETE",
                        f"{url}/_pit",
                        headers=headers,
                        json={"id": pit_id},
                        timeout=30.0,
                    )
                except httpx.HTTPStatusError:
                    pass
                raise SystemExit(1)

            hits = response.json().get("hits", {}).get("hits", [])
            if not hits:
                break

            all_hits.extend(hits)
            search_after = hits[-1].get("sort")

            # Refresh PIT keep-alive
            pit_query["pit"]["id"] = response.json().get("pit_id", pit_id)

    # Step 5: Close PIT
    try:
        httpx.request(
            "DELETE",
            f"{url}/_pit",
            headers=headers,
            json={"id": pit_id},
            timeout=30.0,
        )
    except httpx.HTTPStatusError:
        pass  # Ignore cleanup errors

    console.print(f"[green]Export complete! Total documents: {len(all_hits)}[/green]")

    # Write output
    if output_format == "json":
        result = json.dumps(all_hits, indent=2)
    else:  # jsonl
        result = "\n".join(json.dumps(hit) for hit in all_hits)

    if output:
        output.write_text(result)
        console.print(f"Wrote to {output}")
    else:
        print(result)

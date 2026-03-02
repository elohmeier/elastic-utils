"""Search commands for Elasticsearch async search and export."""

import json
import os
import shutil
import sys
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
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
from .formatting import (
    format_compact_number,
    format_duration_ms,
    format_duration_ns,
    format_hits,
    format_human_number,
    format_shards,
    write_output,
)
from .models import TotalHits

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


def format_total_hits_label(total: TotalHits | int | None) -> str | None:
    """Format total hits for status/progress output when available."""
    if total is None:
        return None
    if isinstance(total, TotalHits):
        value = format_human_number(total.value)
        if total.relation == "gte":
            return f">= {value}"
        return value
    if isinstance(total, int):
        return format_human_number(total)
    return None


def format_total_hits_progress(total: TotalHits | int | None) -> str | None:
    """Format total hits compactly for progress descriptions."""
    if total is None:
        return None
    if isinstance(total, TotalHits):
        value = format_compact_number(total.value)
        if total.relation == "gte":
            return f">={value}"
        return value
    if isinstance(total, int):
        return format_compact_number(total)
    return None


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


def summarize_shard_failures(
    failures: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Aggregate shard failures for fast triage."""
    reason_counts: Counter[str] = Counter()
    node_counts: Counter[str] = Counter()
    index_counts: Counter[str] = Counter()

    for failure in failures:
        reason = failure.get("reason")
        if isinstance(reason, dict):
            reason_type = str(reason.get("type", "unknown"))
        else:
            reason_type = "unknown"
        reason_counts[reason_type] += 1

        node = failure.get("node")
        if isinstance(node, str) and node:
            node_counts[node] += 1
        else:
            node_counts["?"] += 1

        index = failure.get("index")
        if isinstance(index, str) and index:
            index_counts[index] += 1
        else:
            index_counts["?"] += 1

    return {
        "by_reason_type": dict(reason_counts.most_common()),
        "by_node": dict(node_counts.most_common()),
        "by_index": dict(index_counts.most_common()),
    }


def print_shard_failure_summary(
    summary: dict[str, dict[str, int]], *, top_n: int = 10
) -> None:
    """Print compact shard-failure aggregates."""
    console.print("[bold]Failure Summary[/bold]")

    reason_items = list(summary.get("by_reason_type", {}).items())[:top_n]
    node_items = list(summary.get("by_node", {}).items())[:top_n]
    index_items = list(summary.get("by_index", {}).items())[:top_n]

    if reason_items:
        console.print("  by_reason_type:")
        for key, value in reason_items:
            console.print(f"    {key}: {value}")
    if node_items:
        console.print("  by_node:")
        for key, value in node_items:
            console.print(f"    {key}: {value}")
    if index_items:
        console.print("  by_index:")
        for key, value in index_items:
            console.print(f"    {key}: {value}")


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
    client: ElasticsearchClient,
    failures: list[dict[str, Any]],
    payload: dict[str, Any] | None = None,
) -> None:
    """Show current shard routing state for failed shard entries."""
    if payload is None:
        payload = collect_failed_shard_routing(client, failures)
    rows_obj = payload.get("rows", [])
    errors_obj = payload.get("errors", [])
    rows = [row for row in rows_obj if isinstance(row, dict)]
    errors = [err for err in errors_obj if isinstance(err, str)]
    if not rows and not errors:
        console.print(
            "[yellow]Could not infer index/shard targets from failure details.[/yellow]"
        )
        return

    failed_targets: dict[str, set[str]] = {}
    for failure in failures:
        index = failure.get("index")
        shard = failure.get("shard")
        if not isinstance(index, str) or shard is None:
            continue
        failed_targets.setdefault(index, set()).add(str(shard))

    console.print("[bold]Failed Shard Routing[/bold]")
    for row in rows:
        shard = row.get("shard", "?")
        prirep = row.get("prirep", "?")
        state = row.get("state", "?")
        node = row.get("node", "?")
        unassigned = row.get("unassigned_reason") or "-"
        index = row.get("index", "?")
        console.print(
            f"  index={index} shard={shard}{prirep} "
            f"state={state} node={node} unassigned={unassigned}"
        )
    for err in errors:
        console.print(f"[yellow]  {err}[/yellow]")


def print_node_diagnostics(
    client: ElasticsearchClient,
    failures: list[dict[str, Any]],
    payload: dict[str, Any] | None = None,
) -> None:
    """Show key node stats for nodes implicated in shard failures."""
    if payload is None:
        payload = collect_node_diagnostics(client, failures)
    rows_obj = payload.get("rows", [])
    errors_obj = payload.get("errors", [])
    rows = [row for row in rows_obj if isinstance(row, dict)]
    errors = [err for err in errors_obj if isinstance(err, str)]
    if not rows and not errors:
        console.print("[yellow]No node IDs present in shard failure details.[/yellow]")
        return

    console.print("[bold]Impacted Node Stats[/bold]")
    for row in rows:
        console.print(
            "  "
            f"node={row.get('node_name')} ({row.get('node_id')}) "
            f"disk_avail={_format_bytes(row.get('disk_avail_bytes'))} "
            f"disk_total={_format_bytes(row.get('disk_total_bytes'))} "
            f"search_q={row.get('search_queue', '?')} "
            f"search_rejected={row.get('search_rejected', '?')} "
            f"query_total={row.get('query_total', '?')}"
        )
    for err in errors:
        console.print(f"[yellow]  {err}[/yellow]")


def collect_failed_shard_routing(
    client: ElasticsearchClient, failures: list[dict[str, Any]]
) -> dict[str, Any]:
    """Collect routing rows for failed shard targets."""
    nodes = sorted(
        {
            index
            for index in (failure.get("index") for failure in failures)
            if isinstance(index, str) and index
        }
    )
    failed_targets: dict[str, set[str]] = {}
    for failure in failures:
        index = failure.get("index")
        shard = failure.get("shard")
        if not isinstance(index, str) or shard is None:
            continue
        failed_targets.setdefault(index, set()).add(str(shard))

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for index in nodes:
        try:
            response = client.get(
                f"/_cat/shards/{index}",
                params={
                    "format": "json",
                    "h": "index,shard,prirep,state,node,unassigned.reason",
                },
            )
        except SystemExit:
            errors.append(f"Unable to fetch shard routing for index={index}.")
            continue
        if response is None:
            continue

        shard_rows = response.json()
        for row in shard_rows:
            if not isinstance(row, dict):
                continue
            if row.get("shard") not in failed_targets.get(index, set()):
                continue
            rows.append(
                {
                    "index": row.get("index", index),
                    "shard": row.get("shard", "?"),
                    "prirep": row.get("prirep", "?"),
                    "state": row.get("state", "?"),
                    "node": row.get("node", "?"),
                    "unassigned_reason": row.get("unassigned.reason") or "-",
                }
            )
    return {"rows": rows, "errors": errors}


def collect_node_diagnostics(
    client: ElasticsearchClient, failures: list[dict[str, Any]]
) -> dict[str, Any]:
    """Collect key node stats for nodes implicated in shard failures."""
    nodes = sorted(
        {
            node
            for node in (failure.get("node") for failure in failures)
            if isinstance(node, str) and node
        }
    )
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for node_id in nodes:
        try:
            response = client.get(f"/_nodes/{node_id}/stats/fs,indices,thread_pool")
        except SystemExit:
            errors.append(f"Unable to fetch node stats for node={node_id}.")
            continue
        if response is None:
            continue

        payload = response.json()
        nodes_payload = payload.get("nodes", {})
        if not isinstance(nodes_payload, dict) or not nodes_payload:
            errors.append(f"No node payload for node={node_id}.")
            continue

        node = next(iter(nodes_payload.values()))
        if not isinstance(node, dict):
            errors.append(f"Unexpected node payload format for node={node_id}.")
            continue

        fs_total = node.get("fs", {}).get("total", {})
        indices_search = node.get("indices", {}).get("search", {})
        tp_search = node.get("thread_pool", {}).get("search", {})
        rows.append(
            {
                "node_id": node_id,
                "node_name": node.get("name", node_id),
                "disk_avail_bytes": fs_total.get("available_in_bytes"),
                "disk_total_bytes": fs_total.get("total_in_bytes"),
                "search_queue": tp_search.get("queue", "?"),
                "search_rejected": tp_search.get("rejected", "?"),
                "query_total": indices_search.get("query_total", "?"),
            }
        )
    return {"rows": rows, "errors": errors}


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

    worker_id: int
    hits: list[dict[str, Any]]
    last_sort: Any
    next_page_size: int
    page_size: int
    page_duration: float
    payload_bytes: int
    timeout_retries: int


@dataclass
class ExportWorkerDone:
    """Signal that a worker has finished producing chunks."""

    worker_id: int
    success: bool


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


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _query_fingerprint(query: dict[str, Any]) -> str:
    payload = json.dumps(query, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.name}.tmp"
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _load_pyarrow() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        console.print(
            "[red]Parquet support requires pyarrow. Install with:[/red] "
            "[bold]uv sync[/bold]"
        )
        raise SystemExit(1)
    return pa, pq


def _parquet_hit_schema(pa: Any) -> Any:
    return pa.schema(
        [
            pa.field("hit_json", pa.large_string()),
            pa.field("_id", pa.string()),
            pa.field("_index", pa.string()),
        ]
    )


def _hits_to_parquet_table(pa: Any, hits: list[dict[str, Any]]) -> Any:
    schema = _parquet_hit_schema(pa)
    rows = [
        {
            "hit_json": json.dumps(hit, separators=(",", ":")),
            "_id": hit.get("_id"),
            "_index": hit.get("_index"),
        }
        for hit in hits
    ]
    return pa.table(
        {
            "hit_json": pa.array(
                [row["hit_json"] for row in rows], type=pa.large_string()
            ),
            "_id": pa.array([row["_id"] for row in rows], type=pa.string()),
            "_index": pa.array([row["_index"] for row in rows], type=pa.string()),
        },
        schema=schema,
    )


def write_parquet_hits_file(
    *,
    output: Path,
    hits: list[dict[str, Any]],
    compression: str,
    row_group_size: int,
) -> None:
    pa, pq = _load_pyarrow()
    codec = None if compression == "none" else compression
    table = _hits_to_parquet_table(pa, hits)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output.parent / f"{output.name}.tmp"
    pq.write_table(
        table,
        str(tmp_output),
        compression=codec,
        row_group_size=row_group_size,
    )
    os.replace(tmp_output, output)


def assemble_parquet_from_parts(
    *,
    output: Path,
    part_paths: list[Path],
    compression: str,
    row_group_size: int,
) -> None:
    pa, pq = _load_pyarrow()
    codec = None if compression == "none" else compression
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output.parent / f"{output.name}.tmp"
    writer = pq.ParquetWriter(
        str(tmp_output),
        _parquet_hit_schema(pa),
        compression=codec,
    )
    try:
        for part_path in part_paths:
            parquet_file = pq.ParquetFile(str(part_path))
            for batch in parquet_file.iter_batches(batch_size=row_group_size):
                writer.write_batch(batch)
    finally:
        writer.close()
    os.replace(tmp_output, output)


class ParquetExportState:
    """Checkpoint and staged part-file manager for resumable parquet exports."""

    VERSION = 1

    def __init__(
        self,
        *,
        output: Path,
        index: str,
        query: dict[str, Any],
        workers: int,
        compression: str,
        row_group_size: int,
    ) -> None:
        self.output = output
        self.state_dir = output.parent / f"{output.name}.elastic-utils-export-state"
        self.parts_dir = self.state_dir / "parts"
        self.manifest_path = self.state_dir / "manifest.json"
        self.index = index
        self.query_fingerprint = _query_fingerprint(query)
        self.workers = workers
        self.compression = compression
        self.row_group_size = row_group_size
        self._manifest: dict[str, Any] | None = None

    def start(self, *, resume: bool, restart: bool) -> bool:
        if restart and self.state_dir.exists():
            shutil.rmtree(self.state_dir)

        if self.manifest_path.exists():
            if not resume:
                console.print(
                    "[red]Resume state already exists for this output.[/red] "
                    "Use [bold]--resume[/bold] or [bold]--restart[/bold]."
                )
                raise SystemExit(1)
            self._manifest = self._load_manifest()
            self._validate_manifest()
            return True

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self._manifest = self._new_manifest()
        self._save_manifest()
        return False

    def _new_manifest(self) -> dict[str, Any]:
        workers = {
            str(worker_id): {
                "search_after": None,
                "next_page_size": None,
                "docs_written": 0,
                "pages_written": 0,
                "done": False,
            }
            for worker_id in range(self.workers)
        }
        now = _now_utc_iso()
        return {
            "version": self.VERSION,
            "index": self.index,
            "output_file": self.output.name,
            "output_format": "parquet",
            "query_fingerprint": self.query_fingerprint,
            "workers": workers,
            "resolved_workers": self.workers,
            "compression": self.compression,
            "row_group_size": self.row_group_size,
            "docs_written": 0,
            "pages_written": 0,
            "next_part": 1,
            "parts": [],
            "created_at": now,
            "updated_at": now,
        }

    def _load_manifest(self) -> dict[str, Any]:
        try:
            return json.loads(self.manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            console.print(
                "[red]Unable to read existing resume manifest.[/red] "
                "Use [bold]--restart[/bold] to reset state."
            )
            raise SystemExit(1)

    def _validate_manifest(self) -> None:
        manifest = self.manifest
        if manifest.get("version") != self.VERSION:
            console.print(
                "[red]Resume manifest version mismatch.[/red] "
                "Use [bold]--restart[/bold] to reset state."
            )
            raise SystemExit(1)
        checks: list[tuple[str, Any]] = [
            ("index", self.index),
            ("output_format", "parquet"),
            ("query_fingerprint", self.query_fingerprint),
            ("resolved_workers", self.workers),
            ("compression", self.compression),
            ("row_group_size", self.row_group_size),
        ]
        for key, expected in checks:
            if manifest.get(key) != expected:
                console.print(
                    f"[red]Resume state mismatch for {key}.[/red] "
                    "Use [bold]--restart[/bold] to start over."
                )
                raise SystemExit(1)

    @property
    def manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            raise RuntimeError("Manifest not initialized")
        return self._manifest

    @property
    def docs_written(self) -> int:
        return int(self.manifest.get("docs_written", 0))

    @property
    def pages_written(self) -> int:
        return int(self.manifest.get("pages_written", 0))

    def worker_state(self, worker_id: int) -> dict[str, Any]:
        workers = self.manifest.get("workers", {})
        if not isinstance(workers, dict):
            raise RuntimeError("Invalid resume manifest")
        worker = workers.get(str(worker_id))
        if not isinstance(worker, dict):
            raise RuntimeError(f"Missing worker state for worker {worker_id}")
        return worker

    def record_chunk(self, chunk: ExportChunk) -> None:
        manifest = self.manifest
        part_num = int(manifest.get("next_part", 1))
        part_name = f"part-{part_num:08d}.parquet"
        part_path = self.parts_dir / part_name
        write_parquet_hits_file(
            output=part_path,
            hits=chunk.hits,
            compression=self.compression,
            row_group_size=self.row_group_size,
        )

        parts = manifest.get("parts")
        if not isinstance(parts, list):
            raise RuntimeError("Invalid resume manifest parts")
        parts.append(
            {
                "path": f"parts/{part_name}",
                "rows": len(chunk.hits),
                "worker_id": chunk.worker_id,
            }
        )
        manifest["next_part"] = part_num + 1
        manifest["docs_written"] = int(manifest.get("docs_written", 0)) + len(
            chunk.hits
        )
        manifest["pages_written"] = int(manifest.get("pages_written", 0)) + 1

        worker = self.worker_state(chunk.worker_id)
        worker["search_after"] = chunk.last_sort
        worker["next_page_size"] = chunk.next_page_size
        worker["docs_written"] = int(worker.get("docs_written", 0)) + len(chunk.hits)
        worker["pages_written"] = int(worker.get("pages_written", 0)) + 1
        worker["done"] = False
        self._save_manifest()

    def mark_worker_done(self, worker_id: int) -> None:
        worker = self.worker_state(worker_id)
        worker["done"] = True
        self._save_manifest()

    def finalize_output(self) -> None:
        parts_raw = self.manifest.get("parts", [])
        if not isinstance(parts_raw, list):
            raise RuntimeError("Invalid resume manifest parts")
        part_paths: list[Path] = []
        for part in parts_raw:
            if not isinstance(part, dict):
                continue
            rel_path = part.get("path")
            if not isinstance(rel_path, str):
                continue
            part_paths.append(self.state_dir / rel_path)
        assemble_parquet_from_parts(
            output=self.output,
            part_paths=part_paths,
            compression=self.compression,
            row_group_size=self.row_group_size,
        )

    def cleanup(self) -> None:
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)

    def _save_manifest(self) -> None:
        manifest = self.manifest
        manifest["updated_at"] = _now_utc_iso()
        _atomic_write_text(
            self.manifest_path,
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        )


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
    "--request-timeout",
    default=120.0,
    type=float,
    help="Request timeout in seconds (default: 120)",
)
def count(index: str, query_file: Path | None, request_timeout: float) -> None:
    """Count documents matching a query."""
    client = ElasticsearchClient.from_credentials(console)
    query_body = read_query(query_file) if query_file else None
    query_clause = query_body.get("query") if query_body else None

    try:
        result = client.count(index, query=query_clause, timeout=request_timeout)
    except httpx.TimeoutException:
        console.print(
            f"[red]Request timed out after {request_timeout:.0f}s.[/red] "
            "Try increasing [bold]--request-timeout[/bold]."
        )
        raise SystemExit(1)

    console.print(f"Count: [bold]{result.count:,}[/bold]")
    console.print(f"  Shards: {format_shards(result.shards)}")


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
    console.print(f"  Took: {format_duration_ms(result.response.took)}")
    hits_label = format_total_hits_label(result.response.hits.total)
    console.print(f"  Hits returned: {hits_label if hits_label else 'unknown'}")


@search.command()
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text).",
)
def running(output_format: str) -> None:
    """List currently running async searches."""
    client = ElasticsearchClient.from_credentials(console)
    tasks = client.async_search_running_tasks()

    if output_format == "json":
        console.print(json.dumps({"count": len(tasks), "tasks": tasks}, indent=2))
        return

    if not tasks:
        console.print("[green]No running async searches found.[/green]")
        return

    console.print(f"[yellow]Running async searches: {len(tasks)}[/yellow]")
    for task in tasks:
        task_id = task.get("task_id", "?")
        action = task.get("action", "unknown")
        running_nanos = task.get("running_time_in_nanos", 0)
        if not isinstance(running_nanos, int):
            running_nanos = 0
        running_time = format_duration_ns(running_nanos)
        node = task.get("node", "?")
        cancellable = "yes" if task.get("cancellable") else "no"
        description = str(task.get("description", "")).strip()
        console.print(
            f"  {task_id}  {running_time}  {action}  "
            f"node={node} cancellable={cancellable}"
        )
        if description:
            console.print(f"    {description}")


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
@click.option(
    "--summary/--no-summary",
    default=False,
    help="Show aggregated failure counts by reason, node, and index.",
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text).",
)
def debug_shards(
    search_id: str,
    wait_for: str | None,
    deep: bool,
    summary: bool,
    output_format: str,
) -> None:
    """Show shard failure diagnostics for an async search ID."""
    client = ElasticsearchClient.from_credentials(console)
    result = client.async_search_status(search_id, wait_for=wait_for)

    failures = result.response.shards.failures
    failed = result.response.shards.failed
    failure_summary = summarize_shard_failures(failures) if failures else {}
    deep_payload: dict[str, Any] = {}
    if deep and failures:
        deep_payload["routing"] = collect_failed_shard_routing(client, failures)
        deep_payload["nodes"] = collect_node_diagnostics(client, failures)

    if output_format == "json":
        payload: dict[str, Any] = {
            "search_id": search_id,
            "status": "running" if result.is_running else "complete",
            "is_running": result.is_running,
            "is_partial": result.is_partial,
            "shards": {
                "total": result.response.shards.total,
                "successful": result.response.shards.successful,
                "skipped": result.response.shards.skipped,
                "failed": failed,
            },
            "took_ms": result.response.took,
            "failures": failures,
        }
        if summary and failures:
            payload["summary"] = failure_summary
        if deep and failures:
            payload["deep"] = deep_payload
        console.print(json.dumps(payload, indent=2))
        return

    status_color = "yellow" if result.is_running else "green"
    console.print(
        f"[{status_color}]Status: {'Running' if result.is_running else 'Complete'}[/{status_color}]"
    )
    console.print(f"  Partial: {result.is_partial}")
    console.print(f"  Shards: {format_shards(result.response.shards)}")
    console.print(f"  Took: {format_duration_ms(result.response.took)}")

    if failed <= 0:
        console.print("[green]No failed shards reported.[/green]")
        return

    console.print(f"[yellow]Found {failed} failed shard(s).[/yellow]")
    if not failures:
        console.print(
            "[yellow]No detailed failure payload returned by Elasticsearch.[/yellow]"
        )
        return
    if summary:
        print_shard_failure_summary(failure_summary)
    print_shard_failures(failures)
    if deep:
        routing_payload = deep_payload.get("routing")
        nodes_payload = deep_payload.get("nodes")
        if isinstance(routing_payload, dict) and isinstance(nodes_payload, dict):
            rows = routing_payload.get("rows", [])
            errors = routing_payload.get("errors", [])
            if rows or errors:
                print_failed_shard_allocation_debug(
                    client,
                    failures,
                    payload=routing_payload,
                )
            rows = nodes_payload.get("rows", [])
            errors = nodes_payload.get("errors", [])
            if rows or errors:
                print_node_diagnostics(
                    client,
                    failures,
                    payload=nodes_payload,
                )


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
                description=(
                    f"(skipped: {shards.skipped}, failed: {shards.failed})"
                    + (
                        f", hits: {hits_progress}"
                        if (
                            hits_progress := format_total_hits_progress(
                                result.response.hits.total
                            )
                        )
                        else ""
                    )
                ),
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
        console.print(f"  Took: {format_duration_ms(result.response.took)}")
        hits_label = format_total_hits_label(result.response.hits.total)
        console.print(f"  Hits returned: {hits_label if hits_label else 'unknown'}")


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
    "--resume/--no-resume",
    default=True,
    help="Resume interrupted parquet exports from local state (default: enabled)",
)
@click.option(
    "--restart",
    is_flag=True,
    default=False,
    help="Discard existing parquet resume state and start over",
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
    "--worker-progress/--no-worker-progress",
    default=False,
    help="Show per-worker progress tasks (default: disabled)",
)
@click.option(
    "--worker-progress-top-n",
    default=5,
    type=int,
    help="Number of slowest workers to show when --worker-progress is enabled (default: 5)",
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
    resume: bool,
    restart: bool,
    keep_alive: str,
    request_timeout: float,
    max_timeout_retries: int,
    worker_progress: bool,
    worker_progress_top_n: int,
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
    if restart and not resume:
        console.print("[red]--restart cannot be combined with --no-resume.[/red]")
        raise SystemExit(1)
    if request_timeout <= 0:
        console.print("[red]--request-timeout must be greater than 0.[/red]")
        raise SystemExit(1)
    if max_timeout_retries < 0:
        console.print("[red]--max-timeout-retries cannot be negative.[/red]")
        raise SystemExit(1)
    if worker_progress_top_n <= 0:
        console.print("[red]--worker-progress-top-n must be greater than 0.[/red]")
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
                            + (
                                f", hits: {hits_progress}"
                                if (
                                    hits_progress := format_total_hits_progress(
                                        result.response.hits.total
                                    )
                                )
                                else ""
                            )
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
        total_hits_obj = result.response.hits.total if result else None
        if total_hits_obj is None:
            console.print("Initial search complete, total matching docs: unknown")
            console.print(
                '[yellow]Hint:[/yellow] add [bold]"track_total_hits": true[/bold] '
                "to your query for a progress bar with ETA."
            )
        elif isinstance(total_hits_obj, TotalHits) and total_hits_obj.relation == "gte":
            console.print(
                "Initial search complete, total matching docs: "
                f">= {format_human_number(total_docs)}"
            )
            console.print(
                '[yellow]Hint:[/yellow] add [bold]"track_total_hits": true[/bold] '
                "to your query for an accurate progress bar."
            )
        else:
            console.print(
                "Initial search complete, total matching docs: "
                f"{format_human_number(total_docs)}"
            )
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

    parquet_state: ParquetExportState | None = None
    resumed = False
    if output_format == "parquet":
        assert output is not None
        parquet_state = ParquetExportState(
            output=output,
            index=index,
            query=query,
            workers=resolved_workers,
            compression=parquet_compression,
            row_group_size=parquet_row_group_size,
        )
        resumed = parquet_state.start(resume=resume, restart=restart)
        if resumed:
            console.print(
                "Resuming export from existing state "
                f"({parquet_state.pages_written} pages, {parquet_state.docs_written:,} docs)."
            )

    if output_format == "json":
        all_hits: list[dict[str, Any]] = []
        sink: HitSink | None = None
    elif output_format == "jsonl":
        sink = JsonlSink(output)
        all_hits = []
    else:
        sink = None
        all_hits = []

    queue_max_size = max(2 * resolved_workers, 2)
    chunks: Queue[ExportChunk | ExportWorkerDone | Exception] = Queue(
        maxsize=queue_max_size
    )
    stop_event = threading.Event()
    worker_errors: list[str] = []
    lock = threading.Lock()
    aborted = False

    def enqueue_chunk(item: ExportChunk | ExportWorkerDone | Exception) -> None:
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
        failed = False
        try:
            worker_resume_state = (
                parquet_state.worker_state(worker_id) if parquet_state else None
            )
            if worker_resume_state and bool(worker_resume_state.get("done")):
                return

            with local_client.session():
                pit_id = local_client.open_pit(index, keep_alive=keep_alive)
                resumed_page_size = (
                    worker_resume_state.get("next_page_size")
                    if worker_resume_state
                    else None
                )
                if isinstance(resumed_page_size, int) and resumed_page_size > 0:
                    local_page_size = resumed_page_size
                elif adaptive_page_size:
                    local_page_size = max(min(page_size, max_page_size), min_page_size)
                else:
                    local_page_size = page_size
                search_after = (
                    worker_resume_state.get("search_after")
                    if worker_resume_state
                    else None
                )
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
                    last_sort = hits[-1].get("sort")
                    next_page_size = local_page_size
                    if adaptive_page_size:
                        next_page_size = adapt_page_size(
                            local_page_size,
                            page_duration,
                            payload_bytes,
                            len(hits),
                            min_page_size=min_page_size,
                            max_page_size=max_page_size,
                        )
                    enqueue_chunk(
                        ExportChunk(
                            worker_id=worker_id,
                            hits=hits,
                            last_sort=last_sort,
                            next_page_size=next_page_size,
                            page_size=local_page_size,
                            page_duration=page_duration,
                            payload_bytes=payload_bytes,
                            timeout_retries=timeout_failures,
                        )
                    )

                    search_after = last_sort
                    next_pit_id = response.get("pit_id")
                    if isinstance(next_pit_id, str) and next_pit_id:
                        pit_id = next_pit_id

                    local_page_size = next_page_size
        except Exception as e:
            failed = True
            enqueue_chunk(e)
            stop_event.set()
        finally:
            if pit_id:
                try:
                    local_client.close_pit(pit_id)
                except Exception:
                    pass
            enqueue_chunk(ExportWorkerDone(worker_id=worker_id, success=not failed))

    docs_written = parquet_state.docs_written if parquet_state else 0
    pages = parquet_state.pages_written if parquet_state else 0
    recent_page_size = page_size
    fetch_started_at = time.perf_counter()
    recent_docs: deque[tuple[float, int]] = deque()
    worker_docs: dict[int, int] = {}
    for worker_id in range(resolved_workers):
        worker_doc_count = 0
        if parquet_state:
            worker_state = parquet_state.worker_state(worker_id)
            worker_doc_count = int(worker_state.get("docs_written", 0))
        worker_docs[worker_id] = worker_doc_count
    worker_started_at: dict[int, float] = {
        worker_id: fetch_started_at for worker_id in range(resolved_workers)
    }
    worker_last_page_duration: dict[int, float] = {
        worker_id: 0.0 for worker_id in range(resolved_workers)
    }
    worker_last_chunk_at: dict[int, float] = {
        worker_id: fetch_started_at for worker_id in range(resolved_workers)
    }
    worker_retry_counts: dict[int, int] = {
        worker_id: 0 for worker_id in range(resolved_workers)
    }
    worker_finished: set[int] = set()
    total_timeout_retries = 0

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
                "Starting...",
                total=total_docs if total_docs > 0 else None,
                completed=docs_written,
            )
            worker_task_ids: dict[int, int] = {}
            if worker_progress:
                slots = min(worker_progress_top_n, resolved_workers)
                for slot in range(slots):
                    worker_task_ids[slot] = progress.add_task(
                        f"Worker slot {slot + 1}: waiting for data...",
                        total=None,
                    )
            finished_workers = 0
            while finished_workers < resolved_workers:
                try:
                    chunk = chunks.get(timeout=0.2)
                except Empty:
                    continue
                if isinstance(chunk, ExportWorkerDone):
                    if parquet_state and chunk.success:
                        parquet_state.mark_worker_done(chunk.worker_id)
                    worker_finished.add(chunk.worker_id)
                    finished_workers += 1
                    continue

                if isinstance(chunk, Exception):
                    with lock:
                        worker_errors.append(str(chunk))
                    stop_event.set()
                    continue

                pages += 1
                if parquet_state:
                    parquet_state.record_chunk(chunk)
                elif sink:
                    sink.write_hits(chunk.hits)
                else:
                    all_hits.extend(chunk.hits)

                docs_written += len(chunk.hits)
                recent_page_size = chunk.page_size
                total_timeout_retries += chunk.timeout_retries
                worker_docs[chunk.worker_id] += len(chunk.hits)
                worker_last_page_duration[chunk.worker_id] = chunk.page_duration
                worker_last_chunk_at[chunk.worker_id] = time.perf_counter()
                worker_retry_counts[chunk.worker_id] += chunk.timeout_retries

                now = time.perf_counter()
                recent_docs.append((now, len(chunk.hits)))
                while recent_docs and now - recent_docs[0][0] > 30.0:
                    recent_docs.popleft()
                elapsed = max(now - fetch_started_at, 1e-6)
                docs_per_second = docs_written / elapsed

                window_elapsed = now - recent_docs[0][0] if recent_docs else 0.0
                window_docs = sum(doc_count for _, doc_count in recent_docs)
                window_docs_per_second = (
                    window_docs / window_elapsed
                    if window_elapsed > 0
                    else docs_per_second
                )

                worker_rates = [
                    docs / max(now - worker_started_at[worker_id], 1e-6)
                    for worker_id, docs in worker_docs.items()
                    if docs > 0
                ]
                skew = (
                    max(worker_rates) / min(worker_rates)
                    if len(worker_rates) >= 2 and min(worker_rates) > 0
                    else 1.0
                )
                progress.update(
                    task,
                    completed=docs_written,
                    description=(
                        f"Pages {pages} • {docs_written:,} docs"
                        f" • page_size~{recent_page_size}"
                        f" • rate~{format_human_number(int(docs_per_second))}/s"
                        f" (30s {format_human_number(int(window_docs_per_second))}/s)"
                        f" • skew~{skew:.1f}x"
                        f" • retries {total_timeout_retries}"
                    ),
                )
                if worker_progress and worker_task_ids:
                    worker_rows: list[tuple[float, int, str]] = []
                    for worker_id, docs in worker_docs.items():
                        elapsed_worker = max(now - worker_started_at[worker_id], 1e-6)
                        rate = docs / elapsed_worker
                        idle_for = max(now - worker_last_chunk_at[worker_id], 0.0)
                        state = (
                            "done"
                            if worker_id in worker_finished
                            else ("active" if docs > 0 else "starting")
                        )
                        desc = (
                            f"worker {worker_id:02d} • {state}"
                            f" • {docs:,} docs"
                            f" • {format_human_number(int(rate))}/s"
                            f" • last {worker_last_page_duration[worker_id]:.2f}s"
                            f" • idle {idle_for:.1f}s"
                            f" • retries {worker_retry_counts[worker_id]}"
                        )
                        worker_rows.append((rate, worker_id, desc))

                    slowest = sorted(worker_rows, key=lambda row: row[0])[
                        : len(worker_task_ids)
                    ]
                    for slot, task_id in worker_task_ids.items():
                        if slot < len(slowest):
                            _, worker_id, worker_desc = slowest[slot]
                            progress.update(
                                task_id,
                                completed=worker_docs[worker_id],
                                description=worker_desc,
                            )
                        else:
                            progress.update(
                                task_id,
                                description=f"Worker slot {slot + 1}: waiting for data...",
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

    if parquet_state:
        try:
            parquet_state.finalize_output()
            parquet_state.cleanup()
        except Exception as e:
            console.print(f"[red]Export failed while finalizing parquet:[/red] {e}")
            raise SystemExit(1) from e

    final_count = docs_written if docs_written > 0 else len(all_hits)
    console.print(
        f"[green]Export complete! Total documents: {format_human_number(final_count)}[/green]"
    )

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

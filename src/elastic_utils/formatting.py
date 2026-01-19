"""Output formatting utilities."""

import json
from pathlib import Path
from typing import Any

from rich.console import Console

from .models import Shards


def format_shards(shards: Shards) -> str:
    """Format shard progress info."""
    return f"{shards.successful}/{shards.total} (skipped: {shards.skipped}, failed: {shards.failed})"


def format_hits(
    hits: list[dict[str, Any]],
    output_format: str,
) -> str:
    """Format hits as JSON or JSONL."""
    if output_format == "json":
        return json.dumps(hits, indent=2)
    else:  # jsonl
        return "\n".join(json.dumps(hit) for hit in hits)


def write_output(
    content: str,
    output: Path | None,
    console: Console,
    *,
    success_message: str | None = None,
) -> None:
    """Write content to file or stdout."""
    if output:
        output.write_text(content)
        if success_message:
            console.print(success_message)
    else:
        print(content)

"""Output formatting utilities."""

import json
from pathlib import Path
from typing import Any

from rich.console import Console

from .models import Shards


def format_shards(shards: Shards) -> str:
    """Format shard progress info."""
    return f"{shards.successful}/{shards.total} (skipped: {shards.skipped}, failed: {shards.failed})"


def format_compact_number(value: int) -> str:
    """Format large integers using metric suffixes (K, M, B, T)."""
    abs_value = abs(value)
    if abs_value < 1_000:
        return str(value)

    suffixes = (
        (1_000_000_000_000, "T"),
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    )
    for base, suffix in suffixes:
        if abs_value >= base:
            scaled = value / base
            if abs(scaled) >= 100:
                rendered = f"{scaled:.0f}"
            elif abs(scaled) >= 10:
                rendered = f"{scaled:.1f}"
            else:
                rendered = f"{scaled:.2f}"
            return f"{rendered.rstrip('0').rstrip('.')}{suffix}"
    return str(value)


def format_human_number(value: int) -> str:
    """Format integers with compact and exact forms."""
    compact = format_compact_number(value)
    if abs(value) < 1_000:
        return compact
    return f"{compact} ({value:,})"


def format_duration_ms(milliseconds: int) -> str:
    """Format millisecond durations as a human-readable string."""
    if milliseconds < 1_000:
        return f"{milliseconds}ms"
    if milliseconds < 60_000:
        seconds = milliseconds / 1_000
        if seconds >= 10:
            return f"{seconds:.1f}s"
        seconds_text = f"{seconds:.2f}".rstrip("0").rstrip(".")
        return f"{seconds_text}s"

    total_seconds = milliseconds // 1_000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def format_duration_ns(nanoseconds: int) -> str:
    """Format nanosecond durations as a human-readable string."""
    if nanoseconds < 1_000_000:
        return "<1ms"
    milliseconds = int(round(nanoseconds / 1_000_000))
    return format_duration_ms(milliseconds)


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

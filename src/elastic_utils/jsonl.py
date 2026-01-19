"""JSONL file utilities."""

import csv
import json
import re
from pathlib import Path
from typing import Any

import click
from rich.console import Console

console = Console()


def get_nested(obj: dict[str, Any], path: str) -> str | None:
    """Get nested value by dot-notation path (e.g., '_source.host.name')."""
    current: Any = obj
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return str(current) if current is not None else None


def parse_field_spec(spec: str) -> tuple[str, str]:
    """Parse 'path:name' field spec, defaulting name to last path component."""
    if ":" in spec:
        path, name = spec.rsplit(":", 1)
        return path, name
    # Default column name to last path component
    return spec, spec.split(".")[-1]


@click.group()
def jsonl() -> None:
    """JSONL file utilities."""
    pass


@jsonl.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--pattern",
    "-p",
    required=True,
    help="Regex pattern to extract matches from source field.",
)
@click.option(
    "--source-field",
    "-s",
    default="_source.message",
    help="JSON path to search for pattern matches.",
)
@click.option(
    "--field",
    "-f",
    "fields",
    multiple=True,
    help="JSON path to include as column (format: path or path:column_name).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["xlsx", "csv"]),
    default="xlsx",
    help="Output format.",
)
@click.option(
    "--dedupe/--no-dedupe",
    default=True,
    help="Deduplicate rows by match and field values.",
)
@click.option(
    "-o",
    "--output",
    required=True,
    type=click.Path(path_type=Path),
    help="Output file path.",
)
def extract(
    input_file: Path,
    pattern: str,
    source_field: str,
    fields: tuple[str, ...],
    fmt: str,
    dedupe: bool,
    output: Path,
) -> None:
    """Extract regex matches from JSONL to xlsx/csv.

    Searches SOURCE_FIELD for PATTERN matches and outputs them along with
    any additional fields specified via --field options.

    Examples:

        elastic-utils jsonl extract -p 'ID-\\d{4}-[A-Z]+' -f '_source.@timestamp:timestamp' -o out.xlsx data.jsonl

        elastic-utils jsonl extract -p 'ERROR|WARN' -s '_source.level' --format csv -o out.csv logs.jsonl
    """
    # Compile regex
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise click.ClickException(f"Invalid regex pattern: {e}")

    # Parse field specs
    field_specs = [parse_field_spec(f) for f in fields]
    column_names = ["match"] + [name for _, name in field_specs]

    # Collect entries
    entries: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()

    with input_file.open() as f:
        for line_num, line in enumerate(f, 1):
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as e:
                console.print(
                    f"[yellow]Warning: Invalid JSON at line {line_num}: {e}[/yellow]"
                )
                continue

            source_value = get_nested(doc, source_field)
            if source_value is None:
                continue

            matches = regex.findall(source_value)
            for match in matches:
                # Handle groups - if pattern has groups, match is a tuple
                match_str = (
                    match if isinstance(match, str) else match[0] if match else ""
                )

                # Get field values
                field_values = tuple(
                    get_nested(doc, path) or "" for path, _ in field_specs
                )
                row = (match_str,) + field_values

                if dedupe:
                    if row in seen:
                        continue
                    seen.add(row)

                entries.append(row)

    if not entries:
        console.print("[yellow]No matches found.[/yellow]")
        return

    # Sort by first field column (if any) descending, then by match
    if field_specs:
        entries.sort(key=lambda x: (x[1], x[0]), reverse=True)
    else:
        entries.sort(key=lambda x: x[0], reverse=True)

    # Write output
    if fmt == "xlsx":
        _write_xlsx(output, column_names, entries)
    else:
        _write_csv(output, column_names, entries)

    console.print(f"Extracted {len(entries)} entries to {output}")


def _write_xlsx(output: Path, columns: list[str], rows: list[tuple[str, ...]]) -> None:
    """Write data to xlsx file."""
    try:
        import xlsxwriter  # type: ignore[import-untyped]
    except ImportError:
        raise click.ClickException(
            "xlsxwriter is required for xlsx output. "
            "Install with: pip install elastic-utils[xlsx]"
        )

    workbook = xlsxwriter.Workbook(output)
    worksheet = workbook.add_worksheet("Extract")

    # Header format
    header_format = workbook.add_format({"bold": True, "bg_color": "#D9E1F2"})

    # Write headers
    for col, name in enumerate(columns):
        worksheet.write(0, col, name, header_format)

    # Write data
    for row_num, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            worksheet.write(row_num, col, value)

    # Auto-fit column widths (estimate based on content)
    for col, name in enumerate(columns):
        max_len = len(name)
        for row in rows[:100]:  # Sample first 100 rows
            if col < len(row) and row[col]:
                max_len = max(max_len, len(str(row[col])))
        worksheet.set_column(col, col, min(max_len + 2, 50))

    workbook.close()


def _write_csv(output: Path, columns: list[str], rows: list[tuple[str, ...]]) -> None:
    """Write data to csv file."""
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

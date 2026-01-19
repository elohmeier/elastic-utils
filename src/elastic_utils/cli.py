"""Main CLI entry point for elastic-utils."""

import rich_click as click

from .auth import auth
from .describe import describe
from .get import get
from .jsonl import jsonl
from .search import search
from .version import version

click.rich_click.TEXT_MARKUP = "rich"


@click.group()
@click.version_option()
def cli() -> None:
    """Elasticsearch utilities CLI."""
    pass


cli.add_command(auth)
cli.add_command(describe)
cli.add_command(get)
cli.add_command(jsonl)
cli.add_command(search)
cli.add_command(version)

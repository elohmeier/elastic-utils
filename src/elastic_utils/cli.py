"""Main CLI entry point for elastic-utils."""

import rich_click as click

from .auth import auth

click.rich_click.TEXT_MARKUP = "rich"


@click.group()
@click.version_option()
def cli() -> None:
    """Elasticsearch utilities CLI."""
    pass


cli.add_command(auth)

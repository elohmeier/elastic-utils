"""Authentication commands for Elasticsearch."""

import click
import httpx
from rich.console import Console

from .config import (
    delete_credentials,
    get_credentials_path,
    load_credentials,
    save_credentials,
)

console = Console()


@click.group()
def auth() -> None:
    """Manage Elasticsearch authentication."""
    pass


@auth.command()
@click.option("--url", prompt="Elasticsearch URL", help="Elasticsearch server URL")
@click.option("--username", prompt="Username", help="Elasticsearch username")
@click.option(
    "--password",
    prompt="Password",
    hide_input=True,
    help="Elasticsearch password",
)
def login(url: str, username: str, password: str) -> None:
    """Authenticate with Elasticsearch and store an API key."""
    url = url.rstrip("/")

    console.print(f"Authenticating with [bold]{url}[/bold]...")

    try:
        response = httpx.post(
            f"{url}/_security/api_key",
            auth=(username, password),
            json={
                "name": "elastic-utils-cli",
                "expiration": "90d",
            },
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.ConnectError as e:
        console.print(f"[red]Connection error:[/red] {e}")
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            console.print(
                "[red]Authentication failed:[/red] Invalid username or password"
            )
        else:
            console.print(
                f"[red]HTTP error {e.response.status_code}:[/red] {e.response.text}"
            )
        raise SystemExit(1)

    data = response.json()
    api_key_id = data["id"]
    api_key = data["api_key"]

    creds_path = save_credentials(url, api_key_id, api_key)
    console.print("[green]Successfully authenticated![/green]")
    console.print(f"API key stored at: {creds_path}")


@auth.command()
def logout() -> None:
    """Remove stored credentials."""
    if delete_credentials():
        console.print("[green]Credentials removed.[/green]")
    else:
        console.print("[yellow]No credentials found.[/yellow]")


@auth.command()
def status() -> None:
    """Show current authentication status."""
    creds = load_credentials()
    if creds is None:
        console.print("[yellow]Not authenticated.[/yellow]")
        console.print("Run [bold]elastic-utils auth login[/bold] to authenticate.")
        return

    console.print("[green]Authenticated[/green]")
    console.print(f"  URL: {creds['url']}")
    console.print(f"  API Key ID: {creds['api_key_id']}")
    console.print(f"  Created: {creds['created_at']}")
    console.print(f"  Credentials file: {get_credentials_path()}")

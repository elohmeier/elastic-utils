"""Authentication commands for Elasticsearch."""

from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.markup import escape

from .config import (
    delete_credentials,
    get_credentials_path,
    load_credentials,
    save_credentials,
)
from .models import ApiKeyResponse

console = Console()


def _handle_http_error(
    error: httpx.HTTPStatusError,
    custom_messages: dict[int, str] | None = None,
) -> None:
    """Handle HTTP error with optional custom messages per status code."""
    custom_messages = custom_messages or {}
    status_code = error.response.status_code

    if status_code in custom_messages:
        console.print(f"[red]{custom_messages[status_code]}[/red]")
    else:
        console.print(
            f"[red]HTTP error {status_code}:[/red] {escape(error.response.text)}"
        )
    raise SystemExit(1)


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
@click.option(
    "--insecure",
    is_flag=True,
    help="Disable TLS certificate verification (for self-signed/local clusters).",
)
@click.option(
    "--ca-cert",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to CA certificate bundle for TLS verification.",
)
def login(
    url: str,
    username: str,
    password: str,
    insecure: bool,
    ca_cert: Path | None,
) -> None:
    """Authenticate with Elasticsearch and store an API key."""
    if insecure and ca_cert:
        console.print("[red]Use either --insecure or --ca-cert, not both.[/red]")
        raise SystemExit(1)

    url = url.rstrip("/")
    tls_verify: bool | str = True
    if insecure:
        tls_verify = False
    elif ca_cert:
        tls_verify = str(ca_cert)

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
            verify=tls_verify,
        )
        response.raise_for_status()
    except httpx.ConnectError as e:
        console.print(f"[red]Connection error:[/red] {e}")
        raise SystemExit(1)
    except httpx.HTTPStatusError as e:
        _handle_http_error(
            e, {401: "Authentication failed: Invalid username or password"}
        )

    data = ApiKeyResponse.model_validate(response.json())

    creds_path = save_credentials(url, data.id, data.api_key, tls_verify=tls_verify)
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
    tls_verify = creds.get("tls_verify", True)
    if tls_verify is False:
        console.print("  TLS Verify: disabled (--insecure)")
    elif isinstance(tls_verify, str):
        console.print(f"  TLS Verify: CA bundle {tls_verify}")
    else:
        console.print("  TLS Verify: system trust store")
    console.print(f"  Credentials file: {get_credentials_path()}")

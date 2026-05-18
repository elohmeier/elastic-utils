"""Authentication commands for Elasticsearch."""

from pathlib import Path
from typing import Any

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

NEVER_EXPIRE = "never"


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


def _resolve_tls_verify(insecure: bool, ca_cert: Path | None) -> bool | str:
    """Resolve TLS verification setting from CLI flags."""
    if insecure and ca_cert:
        console.print("[red]Use either --insecure or --ca-cert, not both.[/red]")
        raise SystemExit(1)
    if insecure:
        return False
    if ca_cert:
        return str(ca_cert)
    return True


def create_api_key(
    url: str,
    username: str,
    password: str,
    name: str,
    expiration: str | None,
    tls_verify: bool | str,
) -> ApiKeyResponse:
    """Create an Elasticsearch API key.

    Pass ``expiration=None`` (or the sentinel ``"never"``) to create a key
    that never expires.
    """
    body: dict[str, Any] = {"name": name}
    if expiration is not None and expiration != NEVER_EXPIRE:
        body["expiration"] = expiration

    try:
        response = httpx.post(
            f"{url}/_security/api_key",
            auth=(username, password),
            json=body,
            timeout=30.0,
            verify=tls_verify,
        )
        response.raise_for_status()
    except httpx.ConnectError as e:
        console.print(f"[red]Connection error:[/red] {e}")
        raise SystemExit(1) from e
    except httpx.HTTPStatusError as e:
        _handle_http_error(
            e, {401: "Authentication failed: Invalid username or password"}
        )

    return ApiKeyResponse.model_validate(response.json())


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
    "--expiration",
    default="90d",
    show_default=True,
    help=(
        "API key expiration (e.g. '30d', '12h'). "
        f"Use '{NEVER_EXPIRE}' to create a key that never expires."
    ),
)
@click.option(
    "--name",
    default="elastic-utils-cli",
    show_default=True,
    help="API key name.",
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
    expiration: str,
    name: str,
    insecure: bool,
    ca_cert: Path | None,
) -> None:
    """Authenticate with Elasticsearch and store an API key."""
    tls_verify = _resolve_tls_verify(insecure, ca_cert)
    url = url.rstrip("/")

    console.print(f"Authenticating with [bold]{url}[/bold]...")

    data = create_api_key(url, username, password, name, expiration, tls_verify)

    creds_path = save_credentials(url, data.id, data.api_key, tls_verify=tls_verify)
    console.print("[green]Successfully authenticated![/green]")
    console.print(f"API key stored at: {creds_path}")
    if expiration == NEVER_EXPIRE:
        console.print("Expiration: [yellow]never[/yellow]")
    else:
        console.print(f"Expiration: {expiration}")


@auth.command("create-key")
@click.option("--url", prompt="Elasticsearch URL", help="Elasticsearch server URL")
@click.option("--username", prompt="Username", help="Elasticsearch username")
@click.option(
    "--password",
    prompt="Password",
    hide_input=True,
    help="Elasticsearch password",
)
@click.option(
    "--name",
    default="elastic-utils-cli",
    show_default=True,
    help="API key name.",
)
@click.option(
    "--expiration",
    default=NEVER_EXPIRE,
    show_default=True,
    help=(
        "API key expiration (e.g. '30d', '12h'). "
        f"Use '{NEVER_EXPIRE}' to create a key that never expires."
    ),
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
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
def create_key(
    url: str,
    username: str,
    password: str,
    name: str,
    expiration: str,
    output_format: str,
    insecure: bool,
    ca_cert: Path | None,
) -> None:
    """Create an API key and print it to stdout without storing credentials."""
    tls_verify = _resolve_tls_verify(insecure, ca_cert)
    url = url.rstrip("/")

    data = create_api_key(url, username, password, name, expiration, tls_verify)

    if output_format == "json":
        click.echo(data.model_dump_json())
        return

    console.print(f"[bold]ID:[/bold]       {data.id}")
    console.print(f"[bold]Name:[/bold]     {data.name}")
    console.print(f"[bold]API key:[/bold]  {data.api_key}")
    console.print(f"[bold]Encoded:[/bold]  {data.encoded}")
    if data.expiration is None:
        console.print("[bold]Expires:[/bold]  [yellow]never[/yellow]")
    else:
        console.print(f"[bold]Expires:[/bold]  {data.expiration} (epoch ms)")


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

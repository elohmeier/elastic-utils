"""Elasticsearch HTTP client with consistent error handling."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Self

import httpx
from rich.console import Console
from rich.markup import escape

from .config import load_credentials
from .models import AsyncSearchResponse, PITResponse, SearchResponse

if TYPE_CHECKING:
    from collections.abc import Iterator


class ErrorBehavior(Enum):
    """How to handle specific HTTP status codes."""

    EXIT = "exit"  # Print error and raise SystemExit(1)
    RETURN_NONE = "none"  # Return None (for optional resources)
    WARN = "warn"  # Print warning and continue (exit 0)


@dataclass
class StatusHandler:
    """Handler configuration for a specific HTTP status code."""

    behavior: ErrorBehavior
    message: str | None = None


# Pre-configured status handlers for common patterns
NOT_FOUND_EXIT = {404: StatusHandler(ErrorBehavior.EXIT, "Search not found.")}
NOT_FOUND_WARN = {
    404: StatusHandler(
        ErrorBehavior.WARN, "Search not found (may have already expired)."
    )
}
NOT_FOUND_SILENT = {404: StatusHandler(ErrorBehavior.RETURN_NONE)}


class ElasticsearchClient:
    """HTTP client for Elasticsearch with consistent error handling."""

    DEFAULT_TIMEOUT = 30.0

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        console: Console | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.console = console or Console()
        self._client: httpx.Client | None = None

    @classmethod
    def from_credentials(cls, console: Console | None = None) -> Self:
        """Create client from stored credentials, or exit if not authenticated."""
        console = console or Console()
        creds = load_credentials()
        if creds is None:
            console.print("[yellow]Not authenticated.[/yellow]")
            console.print("Run [bold]elastic-utils auth login[/bold] to authenticate.")
            raise SystemExit(1)

        api_key_id = creds["api_key_id"]
        api_key = creds["api_key"]
        encoded = base64.b64encode(f"{api_key_id}:{api_key}".encode()).decode()
        headers = {"Authorization": f"ApiKey {encoded}"}

        return cls(creds["url"], headers, console)

    @contextmanager
    def session(self) -> Iterator[Self]:
        """Context manager for connection pooling with httpx.Client."""
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=self.headers,
        )
        try:
            yield self
        finally:
            self._client.close()
            self._client = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        status_handlers: dict[int, StatusHandler] | None = None,
    ) -> httpx.Response | None:
        """
        Execute an HTTP request with consistent error handling.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: URL path (will be appended to base_url)
            params: Query parameters
            json: JSON body
            timeout: Request timeout in seconds
            status_handlers: Custom handlers for specific HTTP status codes

        Returns:
            httpx.Response on success, or None if handler specifies RETURN_NONE

        Raises:
            SystemExit: On connection error or unhandled HTTP error
        """
        status_handlers = status_handlers or {}

        try:
            if self._client:
                response = self._client.request(
                    method, path, params=params, json=json, timeout=timeout
                )
            else:
                response = httpx.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self.headers,
                    params=params,
                    json=json,
                    timeout=timeout,
                )
            response.raise_for_status()
            return response

        except httpx.ConnectError as e:
            self.console.print(f"[red]Connection error:[/red] {e}")
            raise SystemExit(1)

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code

            if status_code in status_handlers:
                handler = status_handlers[status_code]
                message = handler.message or f"HTTP {status_code}"

                if handler.behavior == ErrorBehavior.EXIT:
                    self.console.print(f"[red]{message}[/red]")
                    raise SystemExit(1)
                elif handler.behavior == ErrorBehavior.RETURN_NONE:
                    return None
                elif handler.behavior == ErrorBehavior.WARN:
                    self.console.print(f"[yellow]{message}[/yellow]")
                    return None

            # Default: print error and exit
            self.console.print(
                f"[red]HTTP error {status_code}:[/red] {escape(e.response.text)}"
            )
            raise SystemExit(1)

    # Convenience methods

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        status_handlers: dict[int, StatusHandler] | None = None,
    ) -> httpx.Response | None:
        """Execute a GET request."""
        return self._request(
            "GET", path, params=params, timeout=timeout, status_handlers=status_handlers
        )

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        status_handlers: dict[int, StatusHandler] | None = None,
    ) -> httpx.Response | None:
        """Execute a POST request."""
        return self._request(
            "POST",
            path,
            params=params,
            json=json,
            timeout=timeout,
            status_handlers=status_handlers,
        )

    def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        status_handlers: dict[int, StatusHandler] | None = None,
    ) -> httpx.Response | None:
        """Execute a DELETE request."""
        return self._request(
            "DELETE",
            path,
            params=params,
            json=json,
            timeout=timeout,
            status_handlers=status_handlers,
        )

    # High-level Elasticsearch operations

    def async_search_submit(
        self,
        index: str,
        query: dict[str, Any],
        *,
        wait_for: str = "1s",
        keep_alive: str = "1h",
    ) -> AsyncSearchResponse:
        """Submit an async search."""
        response = self.post(
            f"/{index}/_async_search",
            params={
                "wait_for_completion_timeout": wait_for,
                "keep_on_completion": "true",
                "keep_alive": keep_alive,
            },
            json=query,
            timeout=60.0,
        )
        assert response is not None
        return AsyncSearchResponse.model_validate(response.json())

    def async_search_status(
        self,
        search_id: str,
        *,
        wait_for: str | None = None,
        timeout: float = 120.0,
    ) -> AsyncSearchResponse:
        """Get async search status. Exits with error if not found."""
        params = {}
        if wait_for:
            params["wait_for_completion_timeout"] = wait_for

        response = self.get(
            f"/_async_search/{search_id}",
            params=params or None,
            timeout=timeout,
            status_handlers=NOT_FOUND_EXIT,
        )
        assert response is not None
        return AsyncSearchResponse.model_validate(response.json())

    def async_search_poll(
        self,
        search_id: str,
        *,
        timeout: float = 30.0,
    ) -> AsyncSearchResponse | None:
        """Poll async search status. Returns None if not found (for wait loops)."""
        response = self.get(
            f"/_async_search/{search_id}",
            timeout=timeout,
            status_handlers=NOT_FOUND_EXIT,
        )
        if response is None:
            return None
        return AsyncSearchResponse.model_validate(response.json())

    def async_search_delete(
        self,
        search_id: str,
        *,
        warn_not_found: bool = False,
        silent: bool = False,
    ) -> bool:
        """Delete an async search. Returns True if deleted."""
        handlers: dict[int, StatusHandler] = {}
        if warn_not_found:
            handlers = NOT_FOUND_WARN
        elif silent:
            handlers = NOT_FOUND_SILENT

        response = self.delete(
            f"/_async_search/{search_id}",
            timeout=30.0,
            status_handlers=handlers,
        )
        return response is not None

    def open_pit(self, index: str, keep_alive: str = "10m") -> str:
        """Open a Point-in-Time and return its ID."""
        response = self.post(
            f"/{index}/_pit",
            params={"keep_alive": keep_alive},
            timeout=60.0,
        )
        assert response is not None
        pit = PITResponse.model_validate(response.json())
        return pit.id

    def close_pit(self, pit_id: str) -> None:
        """Close a Point-in-Time (errors are ignored)."""
        self.delete(
            "/_pit",
            json={"id": pit_id},
            timeout=30.0,
            status_handlers=NOT_FOUND_SILENT,
        )

    def search_with_pit(
        self,
        query: dict[str, Any],
        *,
        timeout: float = 120.0,
    ) -> SearchResponse:
        """Execute a search with PIT (query must include pit field)."""
        response = self.post(
            "/_search",
            json=query,
            timeout=timeout,
        )
        assert response is not None
        return SearchResponse.model_validate(response.json())

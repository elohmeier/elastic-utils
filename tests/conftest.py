"""Pytest configuration and fixtures."""

from __future__ import annotations

import dataclasses
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from pytest_databases._service import DockerService
from pytest_databases.types import ServiceContainer


def _configure_docker_host_for_podman() -> None:
    """Auto-configure DOCKER_HOST for podman when docker CLI is unavailable."""
    if os.environ.get("DOCKER_HOST"):
        return
    if shutil.which("docker"):
        return
    if not shutil.which("podman"):
        return

    socket_path = Path(f"/run/user/{os.getuid()}/podman/podman.sock")
    try:
        subprocess.run(  # noqa: S603
            ["systemctl", "--user", "start", "podman.socket"],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(  # noqa: S603
            ["systemctl", "--user", "start", "podman.service"],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not socket_path.exists():
            time.sleep(0.1)
            continue
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            try:
                sock.connect(str(socket_path))
                break
            except OSError:
                time.sleep(0.1)
    else:
        return

    if socket_path.exists():
        os.environ["DOCKER_HOST"] = f"unix://{socket_path}"


if TYPE_CHECKING:
    from collections.abc import Generator


def pytest_configure() -> None:
    """Prepare test runtime environment before fixtures initialize."""
    _configure_docker_host_for_podman()


@dataclasses.dataclass
class ElasticsearchSecureService(ServiceContainer):
    """Elasticsearch service with security enabled."""

    scheme: str
    user: str
    password: str


@pytest.fixture(scope="session")
def elasticsearch_secure_service(
    docker_service: DockerService,
) -> Generator[ElasticsearchSecureService, None, None]:
    """Elasticsearch 8 with security enabled for API key testing."""
    user = "elastic"
    password = "testpassword123"
    scheme = "http"  # Use HTTP for simplicity in tests

    def check(service: ServiceContainer) -> bool:
        try:
            response = httpx.get(
                f"{scheme}://{service.host}:{service.port}",
                auth=(user, password),
                timeout=5.0,
            )
            return response.status_code == 200
        except Exception:
            return False

    with docker_service.run(
        image="elasticsearch:8.13.0",
        name="elasticsearch-secure",
        container_port=9200,
        env={
            "discovery.type": "single-node",
            "xpack.security.enabled": "true",
            "xpack.security.http.ssl.enabled": "false",
            "xpack.security.transport.ssl.enabled": "false",
            "ELASTIC_PASSWORD": password,
        },
        check=check,
        timeout=120,
        pause=1,
        transient=True,
        mem_limit="1g",
    ) as service:
        yield ElasticsearchSecureService(
            host=service.host,
            port=service.port,
            scheme=scheme,
            user=user,
            password=password,
        )

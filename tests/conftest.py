"""Pytest configuration and fixtures."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import httpx
import pytest

from pytest_databases._service import DockerService
from pytest_databases.types import ServiceContainer

if TYPE_CHECKING:
    from collections.abc import Generator


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

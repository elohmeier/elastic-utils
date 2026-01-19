"""Tests for describe commands."""

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from conftest import ElasticsearchSecureService
from elastic_utils.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    """Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_creds_path(tmp_path: Path):
    """Mock credentials path to use temp directory."""
    creds_file = tmp_path / "credentials.json"
    with patch("elastic_utils.config.get_credentials_path", return_value=creds_file):
        with patch("elastic_utils.config.get_data_dir", return_value=tmp_path):
            yield creds_file


def test_describe_help(runner: CliRunner) -> None:
    """Test describe help command."""
    result = runner.invoke(cli, ["describe", "--help"])
    assert result.exit_code == 0
    assert "index" in result.output
    assert "alias" in result.output


def test_describe_index_not_authenticated(
    runner: CliRunner, mock_creds_path: Path
) -> None:
    """Test describe index when not authenticated."""
    result = runner.invoke(cli, ["describe", "index", "test-index"])
    assert result.exit_code == 1
    assert "Not authenticated" in result.output


def test_describe_index(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
) -> None:
    """Test describe index command."""
    url = f"{elasticsearch_secure_service.scheme}://{elasticsearch_secure_service.host}:{elasticsearch_secure_service.port}"

    # Login first
    result = runner.invoke(
        cli,
        [
            "auth",
            "login",
            "--url",
            url,
            "--username",
            elasticsearch_secure_service.user,
            "--password",
            elasticsearch_secure_service.password,
        ],
    )
    assert result.exit_code == 0

    # Create a test index with some data
    httpx.put(
        f"{url}/test-describe-index",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={
            "mappings": {
                "properties": {
                    "message": {"type": "text"},
                    "@timestamp": {"type": "date"},
                }
            }
        },
        timeout=30.0,
    )

    # Add a document
    httpx.post(
        f"{url}/test-describe-index/_doc",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={"message": "test", "@timestamp": "2024-01-15T12:00:00Z"},
        timeout=30.0,
    )

    httpx.post(
        f"{url}/test-describe-index/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

    # Test describe index
    result = runner.invoke(cli, ["describe", "index", "test-describe-index"])
    assert result.exit_code == 0
    assert "test-describe-index" in result.output
    assert "Name:" in result.output
    assert "Health:" in result.output
    assert "Date Range:" in result.output

    # Test JSON output
    result = runner.invoke(
        cli, ["describe", "index", "test-describe-index", "-o", "json"]
    )
    assert result.exit_code == 0
    output_json = json.loads(result.output)
    assert "index" in output_json
    assert "settings" in output_json
    assert "date_range" in output_json

    # Cleanup
    httpx.delete(
        f"{url}/test-describe-index",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )


def test_describe_alias(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
) -> None:
    """Test describe alias command."""
    url = f"{elasticsearch_secure_service.scheme}://{elasticsearch_secure_service.host}:{elasticsearch_secure_service.port}"

    # Login first
    result = runner.invoke(
        cli,
        [
            "auth",
            "login",
            "--url",
            url,
            "--username",
            elasticsearch_secure_service.user,
            "--password",
            elasticsearch_secure_service.password,
        ],
    )
    assert result.exit_code == 0

    # Create test indices with shared alias
    for i in range(2):
        httpx.put(
            f"{url}/test-describe-alias-{i}",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            json={
                "mappings": {
                    "properties": {
                        "message": {"type": "text"},
                        "@timestamp": {"type": "date"},
                    }
                },
                "aliases": {"test-describe-alias": {}},
            },
            timeout=30.0,
        )

        # Add a document
        httpx.post(
            f"{url}/test-describe-alias-{i}/_doc",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            json={"message": f"test {i}", "@timestamp": f"2024-01-1{i}T12:00:00Z"},
            timeout=30.0,
        )

    httpx.post(
        f"{url}/test-describe-alias/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

    # Test describe alias
    result = runner.invoke(cli, ["describe", "alias", "test-describe-alias"])
    assert result.exit_code == 0
    assert "test-describe-alias" in result.output
    assert "Indices:" in result.output
    assert "test-describe-alias-0" in result.output
    assert "test-describe-alias-1" in result.output
    assert "Date Range:" in result.output

    # Test JSON output
    result = runner.invoke(
        cli, ["describe", "alias", "test-describe-alias", "-o", "json"]
    )
    assert result.exit_code == 0
    output_json = json.loads(result.output)
    assert output_json["alias"] == "test-describe-alias"
    assert "indices" in output_json
    assert "date_range" in output_json

    # Cleanup
    for i in range(2):
        httpx.delete(
            f"{url}/test-describe-alias-{i}",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            timeout=30.0,
        )

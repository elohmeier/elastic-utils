"""Tests for get commands."""

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


def test_get_help(runner: CliRunner) -> None:
    """Test get help command."""
    result = runner.invoke(cli, ["get", "--help"])
    assert result.exit_code == 0
    assert "indices" in result.output
    assert "aliases" in result.output


def test_get_indices_not_authenticated(
    runner: CliRunner, mock_creds_path: Path
) -> None:
    """Test get indices when not authenticated."""
    result = runner.invoke(cli, ["get", "indices"])
    assert result.exit_code == 1
    assert "Not authenticated" in result.output


def test_get_indices(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
) -> None:
    """Test get indices command."""
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

    # Create a test index
    httpx.put(
        f"{url}/test-get-index",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={"mappings": {"properties": {"message": {"type": "text"}}}},
        timeout=30.0,
    )

    # Test get indices
    result = runner.invoke(cli, ["get", "indices"])
    assert result.exit_code == 0
    assert "test-get-index" in result.output

    # Test with pattern
    result = runner.invoke(cli, ["get", "indices", "test-*"])
    assert result.exit_code == 0
    assert "test-get-index" in result.output

    # Test JSON output
    result = runner.invoke(cli, ["get", "indices", "-o", "json"])
    assert result.exit_code == 0
    output_json = json.loads(result.output)
    assert isinstance(output_json, list)
    assert any(idx.get("index") == "test-get-index" for idx in output_json)

    # Test wide output
    result = runner.invoke(cli, ["get", "indices", "-o", "wide"])
    assert result.exit_code == 0
    assert "PRI" in result.output
    assert "REP" in result.output

    # Cleanup
    httpx.delete(
        f"{url}/test-get-index",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )


def test_get_aliases(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
) -> None:
    """Test get aliases command."""
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

    # Create a test index with alias
    httpx.put(
        f"{url}/test-alias-index",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={
            "mappings": {"properties": {"message": {"type": "text"}}},
            "aliases": {"test-alias": {}},
        },
        timeout=30.0,
    )

    # Test get aliases
    result = runner.invoke(cli, ["get", "aliases"])
    assert result.exit_code == 0
    assert "test-alias" in result.output

    # Test JSON output
    result = runner.invoke(cli, ["get", "aliases", "-o", "json"])
    assert result.exit_code == 0
    output_json = json.loads(result.output)
    assert isinstance(output_json, list)
    assert any(a.get("alias") == "test-alias" for a in output_json)

    # Cleanup
    httpx.delete(
        f"{url}/test-alias-index",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

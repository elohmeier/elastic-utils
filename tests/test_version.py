"""Tests for version command."""

import json
from pathlib import Path
from unittest.mock import patch

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


def test_version_not_authenticated(runner: CliRunner, mock_creds_path: Path) -> None:
    """Test version command when not authenticated."""
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 1
    assert "Not authenticated" in result.output


def test_version(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
) -> None:
    """Test version command."""
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

    # Test version command
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert "Cluster:" in result.output
    assert "Version:" in result.output
    assert "Lucene:" in result.output

    # Test JSON output
    result = runner.invoke(cli, ["version", "-o", "json"])
    assert result.exit_code == 0
    output_json = json.loads(result.output)
    assert "cluster_name" in output_json
    assert "version" in output_json
    assert "number" in output_json["version"]

"""Integration tests for auth commands."""

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


def test_auth_status_not_authenticated(
    runner: CliRunner, mock_creds_path: Path
) -> None:
    """Test status command when not authenticated."""
    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 0
    assert "Not authenticated" in result.output


def test_auth_logout_no_credentials(runner: CliRunner, mock_creds_path: Path) -> None:
    """Test logout when no credentials exist."""
    result = runner.invoke(cli, ["auth", "logout"])
    assert result.exit_code == 0
    assert "No credentials found" in result.output


def test_auth_logout_with_credentials(runner: CliRunner, mock_creds_path: Path) -> None:
    """Test logout removes credentials."""
    mock_creds_path.write_text('{"url": "test", "api_key_id": "id", "api_key": "key"}')

    result = runner.invoke(cli, ["auth", "logout"])
    assert result.exit_code == 0
    assert "Credentials removed" in result.output
    assert not mock_creds_path.exists()


def test_auth_status_authenticated(runner: CliRunner, mock_creds_path: Path) -> None:
    """Test status when authenticated."""
    import json

    mock_creds_path.write_text(
        json.dumps(
            {
                "url": "https://localhost:9200",
                "api_key_id": "test-id",
                "api_key": "test-key",
                "created_at": "2026-01-19T12:00:00",
            }
        )
    )

    result = runner.invoke(cli, ["auth", "status"])
    assert result.exit_code == 0
    assert "Authenticated" in result.output
    assert "https://localhost:9200" in result.output
    assert "test-id" in result.output


def test_auth_login_success(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
) -> None:
    """Test successful login creates API key and stores credentials."""
    url = f"{elasticsearch_secure_service.scheme}://{elasticsearch_secure_service.host}:{elasticsearch_secure_service.port}"

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
    assert "Successfully authenticated" in result.output
    assert mock_creds_path.exists()

    # Verify credentials were stored correctly
    import json

    creds = json.loads(mock_creds_path.read_text())
    assert creds["url"] == url
    assert "api_key_id" in creds
    assert "api_key" in creds


def test_auth_login_invalid_credentials(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
) -> None:
    """Test login with invalid credentials."""
    url = f"{elasticsearch_secure_service.scheme}://{elasticsearch_secure_service.host}:{elasticsearch_secure_service.port}"

    result = runner.invoke(
        cli,
        [
            "auth",
            "login",
            "--url",
            url,
            "--username",
            "elastic",
            "--password",
            "wrongpassword",
        ],
    )

    assert result.exit_code == 1
    assert "Authentication failed" in result.output


def test_auth_login_connection_error(runner: CliRunner, mock_creds_path: Path) -> None:
    """Test login with connection error (mocked - can't simulate real connection failure)."""
    with patch("elastic_utils.auth.httpx.post") as mock_post:
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        result = runner.invoke(
            cli,
            [
                "auth",
                "login",
                "--url",
                "http://localhost:9999",
                "--username",
                "user",
                "--password",
                "pass",
            ],
        )

    assert result.exit_code == 1
    assert "Connection error" in result.output

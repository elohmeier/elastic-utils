"""Unit tests for config module."""

from pathlib import Path
from unittest.mock import patch

from elastic_utils.config import (
    delete_credentials,
    load_credentials,
    save_credentials,
)


def test_save_and_load_credentials(tmp_path: Path) -> None:
    """Test saving and loading credentials."""
    creds_file = tmp_path / "credentials.json"

    with patch("elastic_utils.config.get_credentials_path", return_value=creds_file):
        with patch("elastic_utils.config.get_data_dir", return_value=tmp_path):
            # Save credentials
            result_path = save_credentials(
                url="https://localhost:9200",
                api_key_id="test-key-id",
                api_key="test-api-key",
            )

            assert result_path == creds_file
            assert creds_file.exists()

            # Check file permissions (owner read/write only)
            assert (creds_file.stat().st_mode & 0o777) == 0o600

            # Load credentials
            creds = load_credentials()
            assert creds is not None
            assert creds["url"] == "https://localhost:9200"
            assert creds["api_key_id"] == "test-key-id"
            assert creds["api_key"] == "test-api-key"
            assert "created_at" in creds


def test_load_credentials_not_found(tmp_path: Path) -> None:
    """Test loading credentials when file doesn't exist."""
    creds_file = tmp_path / "credentials.json"

    with patch("elastic_utils.config.get_credentials_path", return_value=creds_file):
        creds = load_credentials()
        assert creds is None


def test_delete_credentials(tmp_path: Path) -> None:
    """Test deleting credentials."""
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}")

    with patch("elastic_utils.config.get_credentials_path", return_value=creds_file):
        assert delete_credentials() is True
        assert not creds_file.exists()


def test_delete_credentials_not_found(tmp_path: Path) -> None:
    """Test deleting credentials when file doesn't exist."""
    creds_file = tmp_path / "credentials.json"

    with patch("elastic_utils.config.get_credentials_path", return_value=creds_file):
        assert delete_credentials() is False

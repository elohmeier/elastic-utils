"""Tests for search commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


@pytest.fixture
def authenticated_creds(mock_creds_path: Path) -> Path:
    """Set up authenticated credentials."""
    mock_creds_path.write_text(
        json.dumps(
            {
                "url": "http://localhost:9200",
                "api_key_id": "test-id",
                "api_key": "test-key",
                "created_at": "2026-01-19T12:00:00",
            }
        )
    )
    return mock_creds_path


def test_search_help(runner: CliRunner) -> None:
    """Test search help command."""
    result = runner.invoke(cli, ["search", "--help"])
    assert result.exit_code == 0
    assert "submit" in result.output
    assert "status" in result.output
    assert "wait" in result.output
    assert "get" in result.output
    assert "delete" in result.output
    assert "export" in result.output


def test_search_submit_not_authenticated(
    runner: CliRunner, mock_creds_path: Path
) -> None:
    """Test submit command when not authenticated."""
    result = runner.invoke(cli, ["search", "submit", "--index", "test"])
    assert result.exit_code == 1
    assert "Not authenticated" in result.output


def test_search_submit_no_query(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test submit command without query input (empty stdin)."""
    result = runner.invoke(cli, ["search", "submit", "--index", "test"], input="")
    assert result.exit_code == 1
    assert "No query provided" in result.output


def test_search_submit_with_query_file(
    runner: CliRunner, authenticated_creds: Path, tmp_path: Path
) -> None:
    """Test submit command with query file."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query": {"match_all": {}}}')

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-search-id",
        "is_running": True,
        "is_partial": True,
        "response": {
            "_shards": {"total": 10, "successful": 3, "skipped": 0, "failed": 0}
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(
            cli,
            [
                "search",
                "submit",
                "--index",
                "test-index",
                "--query-file",
                str(query_file),
            ],
        )

    assert result.exit_code == 0
    assert "Search submitted" in result.output
    assert "test-search-id" in result.output


def test_search_submit_with_stdin(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test submit command with stdin input."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "stdin-search-id",
        "is_running": True,
        "is_partial": True,
        "response": {
            "_shards": {"total": 5, "successful": 0, "skipped": 0, "failed": 0}
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(
            cli,
            ["search", "submit", "--index", "test-index"],
            input='{"query": {"match_all": {}}}',
        )

    assert result.exit_code == 0
    assert "Search submitted" in result.output
    assert "stdin-search-id" in result.output


def test_search_submit_invalid_json(
    runner: CliRunner, authenticated_creds: Path, tmp_path: Path
) -> None:
    """Test submit command with invalid JSON."""
    query_file = tmp_path / "query.json"
    query_file.write_text("not valid json")

    result = runner.invoke(
        cli,
        ["search", "submit", "--index", "test-index", "--query-file", str(query_file)],
    )

    assert result.exit_code == 1
    assert "Invalid JSON" in result.output


def test_search_status_not_found(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test status command for non-existent search."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not found", request=MagicMock(), response=mock_response
    )

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "status", "nonexistent-id"])

    assert result.exit_code == 1
    assert "Search not found" in result.output


def test_search_status_success(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test status command success."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {"total": 10, "successful": 10, "skipped": 0, "failed": 0},
            "took": 1234,
            "hits": {"hits": [{"_id": "1"}, {"_id": "2"}]},
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "status", "test-id"])

    assert result.exit_code == 0
    assert "Complete" in result.output
    assert "10/10" in result.output
    assert "1234ms" in result.output


def test_search_get_jsonl_output(
    runner: CliRunner, authenticated_creds: Path, tmp_path: Path
) -> None:
    """Test get command with JSONL output."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "response": {
            "hits": {
                "hits": [
                    {"_id": "1", "_source": {"message": "test1"}},
                    {"_id": "2", "_source": {"message": "test2"}},
                ]
            }
        },
    }

    output_file = tmp_path / "output.jsonl"

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(
            cli, ["search", "get", "test-id", "--output", str(output_file)]
        )

    assert result.exit_code == 0
    assert "2 hits" in result.output
    assert output_file.exists()

    lines = output_file.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["_id"] == "1"


def test_search_get_json_output(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test get command with JSON output to stdout."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "response": {
            "hits": {
                "hits": [
                    {"_id": "1", "_source": {"message": "test1"}},
                ]
            }
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "get", "test-id", "--format", "json"])

    assert result.exit_code == 0
    # Output should be valid JSON
    output_json = json.loads(result.output)
    assert len(output_json) == 1
    assert output_json[0]["_id"] == "1"


def test_search_delete_success(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test delete command success."""
    mock_response = MagicMock()

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "delete", "test-id"])

    assert result.exit_code == 0
    assert "Search deleted" in result.output


def test_search_delete_not_found(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test delete command for non-existent search."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not found", request=MagicMock(), response=mock_response
    )

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "delete", "nonexistent-id"])

    assert result.exit_code == 0
    assert "not found" in result.output.lower()


def test_search_wait_success(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test wait command until search completes."""
    # First call: still running
    running_response = MagicMock()
    running_response.json.return_value = {
        "id": "test-id",
        "is_running": True,
        "is_partial": True,
        "response": {
            "_shards": {"total": 10, "successful": 5, "skipped": 0, "failed": 0}
        },
    }

    # Second call: complete
    complete_response = MagicMock()
    complete_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {"total": 10, "successful": 10, "skipped": 0, "failed": 0},
            "took": 5000,
            "hits": {"hits": [{"_id": "1"}]},
        },
    }

    with patch(
        "elastic_utils.client.httpx.request",
        side_effect=[running_response, complete_response],
    ):
        with patch("elastic_utils.search.time.sleep"):  # Skip actual waiting
            result = runner.invoke(
                cli, ["search", "wait", "test-id", "--interval", "1"]
            )

    assert result.exit_code == 0
    assert "Search complete" in result.output


def test_search_connection_error(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test handling of connection errors."""
    with patch("elastic_utils.client.httpx.request") as mock_request:
        mock_request.side_effect = httpx.ConnectError("Connection refused")

        result = runner.invoke(cli, ["search", "status", "test-id"])

    assert result.exit_code == 1
    assert "Connection error" in result.output


# Integration tests with real Elasticsearch


def test_search_submit_integration(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
    tmp_path: Path,
) -> None:
    """Test submit command against real Elasticsearch."""
    # First login to get real credentials
    url = f"{elasticsearch_secure_service.scheme}://{elasticsearch_secure_service.host}:{elasticsearch_secure_service.port}"

    login_result = runner.invoke(
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
    assert login_result.exit_code == 0

    # Create a test index with some data
    import httpx as real_httpx

    real_httpx.put(
        f"{url}/test-index",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={"mappings": {"properties": {"message": {"type": "text"}}}},
        timeout=30.0,
    )

    real_httpx.post(
        f"{url}/test-index/_doc",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={"message": "test document"},
        timeout=30.0,
    )

    real_httpx.post(
        f"{url}/test-index/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

    # Create query file
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query": {"match_all": {}}, "size": 10}')

    # Submit async search
    result = runner.invoke(
        cli,
        ["search", "submit", "--index", "test-index", "--query-file", str(query_file)],
    )

    assert result.exit_code == 0
    assert "Search submitted" in result.output


def test_search_export_integration(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
    tmp_path: Path,
) -> None:
    """Test export command against real Elasticsearch."""
    # First login to get real credentials
    url = f"{elasticsearch_secure_service.scheme}://{elasticsearch_secure_service.host}:{elasticsearch_secure_service.port}"

    login_result = runner.invoke(
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
    assert login_result.exit_code == 0

    # Create a test index with some data
    import httpx as real_httpx

    # Delete index if exists
    try:
        real_httpx.delete(
            f"{url}/export-test-index",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            timeout=30.0,
        )
    except Exception:
        pass

    real_httpx.put(
        f"{url}/export-test-index",
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

    # Add some documents
    for i in range(5):
        real_httpx.post(
            f"{url}/export-test-index/_doc",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            json={
                "message": f"test document {i}",
                "@timestamp": f"2026-01-19T12:00:0{i}Z",
            },
            timeout=30.0,
        )

    real_httpx.post(
        f"{url}/export-test-index/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

    # Create query file
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query": {"match_all": {}}}')

    output_file = tmp_path / "export.jsonl"

    # Run export
    result = runner.invoke(
        cli,
        [
            "search",
            "export",
            "--index",
            "export-test-index",
            "--query-file",
            str(query_file),
            "--output",
            str(output_file),
            "--page-size",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "Export complete" in result.output
    assert "5" in result.output  # Should have 5 documents

    # Verify output file
    assert output_file.exists()
    lines = output_file.read_text().strip().split("\n")
    assert len(lines) == 5

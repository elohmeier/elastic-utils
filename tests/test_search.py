"""Tests for search commands."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from conftest import ElasticsearchSecureService
from elastic_utils.cli import cli
from elastic_utils.search import adapt_page_size, infer_input_format


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
    assert "count" in result.output
    assert "status" in result.output
    assert "wait" in result.output
    assert "debug-shards" in result.output
    assert "get" in result.output
    assert "delete" in result.output
    assert "export" in result.output
    assert "import" in result.output


def test_adapt_page_size_scales_up() -> None:
    """Adaptive pager should increase page size for fast/small responses."""
    new_size = adapt_page_size(
        1000,
        page_duration=0.3,
        payload_bytes=2_000_000,
        returned_hits=1000,
        min_page_size=250,
        max_page_size=5000,
    )
    assert new_size > 1000


def test_adapt_page_size_scales_down() -> None:
    """Adaptive pager should reduce page size for slow/large responses."""
    new_size = adapt_page_size(
        1000,
        page_duration=3.0,
        payload_bytes=30_000_000,
        returned_hits=1000,
        min_page_size=250,
        max_page_size=5000,
    )
    assert new_size < 1000


def test_infer_input_format_from_extension(tmp_path: Path) -> None:
    """Import format should infer parquet from file extension."""
    parquet_file = tmp_path / "input.parquet"
    assert infer_input_format(parquet_file, None) == "parquet"
    assert infer_input_format(parquet_file, "jsonl") == "jsonl"


def test_search_submit_not_authenticated(
    runner: CliRunner, mock_creds_path: Path
) -> None:
    """Test submit command when not authenticated."""
    result = runner.invoke(cli, ["search", "submit", "--index", "test"])
    assert result.exit_code == 1
    assert "Not authenticated" in result.output


def test_search_export_parquet_requires_output(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Parquet export should require file output."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query":{"match_all":{}}}')
    result = runner.invoke(
        cli,
        [
            "search",
            "export",
            "--index",
            "source-index",
            "--query-file",
            str(query_file),
            "--format",
            "parquet",
        ],
    )
    assert result.exit_code == 1
    assert "Parquet export requires --output" in result.output


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
            "_shards": {"total": 10, "successful": 3, "skipped": 0, "failed": 0},
            "hits": {"hits": []},
            "took": 100,
            "timed_out": False,
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
            "_shards": {"total": 5, "successful": 0, "skipped": 0, "failed": 0},
            "hits": {"hits": []},
            "took": 50,
            "timed_out": False,
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


def test_search_count_with_query_file(
    runner: CliRunner, authenticated_creds: Path, tmp_path: Path
) -> None:
    """Test count command with a query file."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query": {"match_all": {}}, "size": 10}')

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "count": 42,
        "_shards": {"total": 5, "successful": 5, "skipped": 0, "failed": 0},
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(
            cli,
            [
                "search",
                "count",
                "--index",
                "test-index",
                "--query-file",
                str(query_file),
            ],
        )

    assert result.exit_code == 0
    assert "42" in result.output
    assert "5/5" in result.output


def test_search_count_without_query(
    runner: CliRunner, authenticated_creds: Path
) -> None:
    """Test count command without query file (match_all)."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "count": 1000,
        "_shards": {"total": 3, "successful": 3, "skipped": 0, "failed": 0},
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(
            cli,
            ["search", "count", "--index", "test-index"],
        )

    assert result.exit_code == 0
    assert "1,000" in result.output


def test_search_count_not_authenticated(
    runner: CliRunner, mock_creds_path: Path
) -> None:
    """Test count command when not authenticated."""
    result = runner.invoke(cli, ["search", "count", "--index", "test"])
    assert result.exit_code == 1
    assert "Not authenticated" in result.output


def test_search_count_timeout(runner: CliRunner, authenticated_creds: Path) -> None:
    """Test count command handles timeout gracefully."""
    with patch("elastic_utils.client.httpx.request") as mock_request:
        mock_request.side_effect = httpx.ReadTimeout("timed out")

        result = runner.invoke(
            cli,
            ["search", "count", "--index", "test-index", "--request-timeout", "10"],
        )

    assert result.exit_code == 1
    assert "timed out" in result.output.lower()
    assert "--request-timeout" in result.output


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
            "timed_out": False,
            "hits": {"hits": [{"_id": "1"}, {"_id": "2"}]},
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "status", "test-id"])

    assert result.exit_code == 0
    assert "Complete" in result.output
    assert "10/10" in result.output
    assert "1234ms" in result.output


def test_search_debug_shards_with_failures(
    runner: CliRunner, authenticated_creds: Path
) -> None:
    """Debug-shards command should print shard failure details."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {
                "total": 10,
                "successful": 9,
                "skipped": 0,
                "failed": 1,
                "failures": [
                    {
                        "index": "logs-0001",
                        "shard": 3,
                        "node": "node-a",
                        "reason": {
                            "type": "query_shard_exception",
                            "reason": "No mapping found for [@timestamp] in order to sort on",
                        },
                    }
                ],
            },
            "took": 1234,
            "timed_out": False,
            "hits": {"hits": []},
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "debug-shards", "test-id"])

    assert result.exit_code == 0
    assert "Found 1 failed shard(s)" in result.output
    assert "index=logs-0001 shard=3 node=node-a" in result.output
    assert "query_shard_exception" in result.output


def test_search_debug_shards_no_failures(
    runner: CliRunner, authenticated_creds: Path
) -> None:
    """Debug-shards command should report clean shard state."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {"total": 10, "successful": 10, "skipped": 0, "failed": 0},
            "took": 1234,
            "timed_out": False,
            "hits": {"hits": []},
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "debug-shards", "test-id"])

    assert result.exit_code == 0
    assert "No failed shards reported" in result.output


def test_search_debug_shards_summary_text(
    runner: CliRunner, authenticated_creds: Path
) -> None:
    """Debug-shards summary mode should print grouped counts."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {
                "total": 10,
                "successful": 8,
                "skipped": 0,
                "failed": 2,
                "failures": [
                    {
                        "index": "logs-0001",
                        "shard": 1,
                        "node": "node-a",
                        "reason": {"type": "i_o_exception", "reason": "cache read"},
                    },
                    {
                        "index": "logs-0002",
                        "shard": 2,
                        "node": "node-a",
                        "reason": {"type": "i_o_exception", "reason": "cache read"},
                    },
                ],
            },
            "took": 1234,
            "timed_out": False,
            "hits": {"hits": []},
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "debug-shards", "test-id", "--summary"])

    assert result.exit_code == 0
    assert "Failure Summary" in result.output
    assert "by_reason_type" in result.output
    assert "i_o_exception: 2" in result.output
    assert "node-a: 2" in result.output


def test_search_debug_shards_json_output(
    runner: CliRunner, authenticated_creds: Path
) -> None:
    """Debug-shards JSON output should return machine-readable diagnostics."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {
                "total": 10,
                "successful": 9,
                "skipped": 0,
                "failed": 1,
                "failures": [
                    {
                        "index": "logs-0001",
                        "shard": 3,
                        "node": "node-a",
                        "reason": {
                            "type": "query_shard_exception",
                            "reason": "bad query",
                        },
                    }
                ],
            },
            "took": 1234,
            "timed_out": False,
            "hits": {"hits": []},
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(
            cli,
            ["search", "debug-shards", "test-id", "--output", "json", "--summary"],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["search_id"] == "test-id"
    assert payload["status"] == "complete"
    assert payload["shards"]["failed"] == 1
    assert payload["summary"]["by_reason_type"]["query_shard_exception"] == 1


def test_search_debug_shards_deep_fetches_extra_diagnostics(
    runner: CliRunner, authenticated_creds: Path
) -> None:
    """Debug-shards deep mode should fetch routing and node diagnostics."""
    status_response = MagicMock()
    status_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {
                "total": 10,
                "successful": 9,
                "skipped": 0,
                "failed": 1,
                "failures": [
                    {
                        "index": "logs-0001",
                        "shard": 3,
                        "node": "node-a",
                        "reason": {
                            "type": "i_o_exception",
                            "reason": "cache read failed",
                        },
                    }
                ],
            },
            "took": 1234,
            "timed_out": False,
            "hits": {"hits": []},
        },
    }
    shard_response = MagicMock()
    shard_response.json.return_value = [
        {
            "index": "logs-0001",
            "shard": "3",
            "prirep": "p",
            "state": "STARTED",
            "node": "node-a",
            "unassigned.reason": "",
        }
    ]
    node_response = MagicMock()
    node_response.json.return_value = {
        "nodes": {
            "node-a": {
                "name": "data-hot-1",
                "fs": {
                    "total": {
                        "available_in_bytes": 1073741824,
                        "total_in_bytes": 2147483648,
                    }
                },
                "indices": {"search": {"query_total": 123}},
                "thread_pool": {"search": {"queue": 4, "rejected": 2}},
            }
        }
    }

    def _mock_request(*args, **kwargs):
        url = kwargs.get("url") or (args[1] if len(args) > 1 else "")
        if url.endswith("/_async_search/test-id"):
            return status_response
        if url.endswith("/_cat/shards/logs-0001"):
            return shard_response
        if url.endswith("/_nodes/node-a/stats/fs,indices,thread_pool"):
            return node_response
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("elastic_utils.client.httpx.request", side_effect=_mock_request):
        result = runner.invoke(cli, ["search", "debug-shards", "test-id", "--deep"])

    assert result.exit_code == 0
    assert "Failed Shard Routing" in result.output
    assert "index=logs-0001 shard=3p state=STARTED node=node-a" in result.output
    assert "Impacted Node Stats" in result.output
    assert "node=data-hot-1 (node-a)" in result.output


def test_search_get_jsonl_output(
    runner: CliRunner, authenticated_creds: Path, tmp_path: Path
) -> None:
    """Test get command with JSONL output."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "took": 10,
            "timed_out": False,
            "hits": {
                "hits": [
                    {"_id": "1", "_source": {"message": "test1"}},
                    {"_id": "2", "_source": {"message": "test2"}},
                ]
            },
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
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "took": 10,
            "timed_out": False,
            "hits": {
                "hits": [
                    {"_id": "1", "_source": {"message": "test1"}},
                ]
            },
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "get", "test-id", "--format", "json"])

    assert result.exit_code == 0
    # Output should be valid JSON
    output_json = json.loads(result.output)
    assert len(output_json) == 1
    assert output_json[0]["_id"] == "1"


def test_search_get_warns_when_total_hits_but_no_docs(
    runner: CliRunner, authenticated_creds: Path
) -> None:
    """Get should explain empty docs when total hits are present."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "test-id",
        "is_running": False,
        "is_partial": False,
        "response": {
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "took": 10,
            "timed_out": False,
            "hits": {
                "total": {"value": 10000, "relation": "gte"},
                "hits": [],
            },
        },
    }

    with patch("elastic_utils.client.httpx.request", return_value=mock_response):
        result = runner.invoke(cli, ["search", "get", "test-id", "--format", "json"])

    assert result.exit_code == 0
    assert "matched documents but returned zero hit documents" in result.output
    assert "[]" in result.output


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
            "_shards": {"total": 10, "successful": 5, "skipped": 0, "failed": 0},
            "hits": {"hits": []},
            "took": 1000,
            "timed_out": False,
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
            "timed_out": False,
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


def test_search_import_success(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Test import command with successful bulk create."""
    input_file = tmp_path / "input.jsonl"
    input_file.write_text('{"_id":"1","_source":{"message":"test"}}\n')

    mock_client = MagicMock()
    mock_client.index_exists.return_value = True
    mock_client.bulk.return_value = {"items": [{"create": {"_id": "1", "status": 201}}]}
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None

    with patch("elastic_utils.search.create_client", return_value=mock_client):
        result = runner.invoke(
            cli,
            [
                "search",
                "import",
                "--index",
                "dest-index",
                "--input",
                str(input_file),
                "--url",
                "http://dest:9200",
                "--api-key-id",
                "id",
                "--api-key",
                "key",
            ],
        )

    assert result.exit_code == 0
    assert "Import complete" in result.output
    assert "Created: 1" in result.output


def test_search_import_conflict_skipped(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Test import command skips conflicts and exits successfully."""
    input_file = tmp_path / "input.jsonl"
    input_file.write_text('{"_id":"1","_source":{"message":"test"}}\n')

    mock_client = MagicMock()
    mock_client.index_exists.return_value = True
    mock_client.bulk.return_value = {"items": [{"create": {"_id": "1", "status": 409}}]}
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None

    with patch("elastic_utils.search.create_client", return_value=mock_client):
        result = runner.invoke(
            cli,
            [
                "search",
                "import",
                "--index",
                "dest-index",
                "--input",
                str(input_file),
                "--url",
                "http://dest:9200",
                "--api-key-id",
                "id",
                "--api-key",
                "key",
            ],
        )

    assert result.exit_code == 0
    assert "Conflicts skipped: 1" in result.output


def test_search_import_non_conflict_failure(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Test import command exits with error for non-conflict bulk failures."""
    input_file = tmp_path / "input.jsonl"
    input_file.write_text('{"_id":"1","_source":{"message":"test"}}\n')

    mock_client = MagicMock()
    mock_client.index_exists.return_value = True
    mock_client.bulk.return_value = {
        "items": [
            {
                "create": {
                    "_id": "1",
                    "status": 400,
                    "error": {
                        "type": "mapper_parsing_exception",
                        "reason": "bad field",
                    },
                }
            }
        ]
    }
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None

    with patch("elastic_utils.search.create_client", return_value=mock_client):
        result = runner.invoke(
            cli,
            [
                "search",
                "import",
                "--index",
                "dest-index",
                "--input",
                str(input_file),
                "--url",
                "http://dest:9200",
                "--api-key-id",
                "id",
                "--api-key",
                "key",
            ],
        )

    assert result.exit_code == 1
    assert "Bulk item failed" in result.output
    assert "Failed: 1" in result.output


def test_search_import_missing_source_field(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Test import command validates required _source field."""
    input_file = tmp_path / "input.jsonl"
    input_file.write_text('{"_id":"1"}\n')

    mock_client = MagicMock()
    mock_client.index_exists.return_value = True
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None

    with patch("elastic_utils.search.create_client", return_value=mock_client):
        result = runner.invoke(
            cli,
            [
                "search",
                "import",
                "--index",
                "dest-index",
                "--input",
                str(input_file),
                "--url",
                "http://dest:9200",
                "--api-key-id",
                "id",
                "--api-key",
                "key",
            ],
        )

    assert result.exit_code == 1
    assert "missing object '_source' field" in result.output


def test_search_export_with_explicit_auth(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Test export command works with explicit auth options."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query":{"match_all":{}}}')
    output_file = tmp_path / "output.jsonl"

    shards = MagicMock()
    shards.total = 1
    shards.successful = 1
    shards.skipped = 0
    shards.failed = 0

    poll_result = MagicMock()
    poll_result.is_running = False
    poll_result.response.shards = shards
    poll_result.total_hits = 1

    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None
    mock_client.async_search_submit.return_value.id = "search-id"
    mock_client.async_search_poll.side_effect = [poll_result]
    mock_client.primary_shard_count.return_value = 1
    mock_client.search_with_pit_raw.side_effect = [
        {
            "hits": {
                "hits": [{"_id": "1", "_source": {"message": "test"}, "sort": [1]}]
            },
            "pit_id": "pit-2",
        },
        {"hits": {"hits": []}},
    ]

    with patch("elastic_utils.search.create_client", return_value=mock_client):
        result = runner.invoke(
            cli,
            [
                "search",
                "export",
                "--index",
                "source-index",
                "--query-file",
                str(query_file),
                "--output",
                str(output_file),
                "--url",
                "http://source:9200",
                "--api-key-id",
                "id",
                "--api-key",
                "key",
            ],
        )

    assert result.exit_code == 0
    assert "Async search ID: search-id" in result.output
    assert "Export complete" in result.output
    assert output_file.exists()


def test_search_export_retries_read_timeout(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Export should retry PIT page fetches after read timeout."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query":{"match_all":{}}}')
    output_file = tmp_path / "output.jsonl"

    shards = MagicMock()
    shards.total = 1
    shards.successful = 1
    shards.skipped = 0
    shards.failed = 0
    shards.failures = []

    poll_result = MagicMock()
    poll_result.is_running = False
    poll_result.response.shards = shards
    poll_result.total_hits = 1

    timeout_exc = httpx.ReadTimeout(
        "timed out",
        request=httpx.Request("POST", "http://source:9200/_search"),
    )

    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None
    mock_client.async_search_submit.return_value.id = "search-id"
    mock_client.async_search_poll.side_effect = [poll_result]
    mock_client.primary_shard_count.return_value = 1
    mock_client.search_with_pit_raw.side_effect = [
        timeout_exc,
        {
            "hits": {
                "hits": [{"_id": "1", "_source": {"message": "test"}, "sort": [1]}]
            },
            "pit_id": "pit-2",
        },
        {"hits": {"hits": []}},
    ]

    with (
        patch("elastic_utils.search.create_client", return_value=mock_client),
        patch("elastic_utils.search.time.sleep", return_value=None),
    ):
        result = runner.invoke(
            cli,
            [
                "search",
                "export",
                "--index",
                "source-index",
                "--query-file",
                str(query_file),
                "--output",
                str(output_file),
                "--max-timeout-retries",
                "2",
            ],
        )

    assert result.exit_code == 0
    assert "Export complete" in result.output
    assert output_file.exists()


def test_search_export_no_adaptive_uses_requested_page_size(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Export should honor --page-size when adaptive sizing is disabled."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query":{"match_all":{}}}')
    output_file = tmp_path / "output.jsonl"

    shards = MagicMock()
    shards.total = 1
    shards.successful = 1
    shards.skipped = 0
    shards.failed = 0
    shards.failures = []

    poll_result = MagicMock()
    poll_result.is_running = False
    poll_result.response.shards = shards
    poll_result.total_hits = 1

    first_request_size: int | None = None

    def fake_search_with_pit_raw(
        request: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        nonlocal first_request_size
        if first_request_size is None:
            first_request_size = request.get("size")
            return {
                "hits": {
                    "hits": [{"_id": "1", "_source": {"message": "test"}, "sort": [1]}]
                },
                "pit_id": "pit-2",
            }
        return {"hits": {"hits": []}}

    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None
    mock_client.async_search_submit.return_value.id = "search-id"
    mock_client.async_search_poll.side_effect = [poll_result]
    mock_client.primary_shard_count.return_value = 1
    mock_client.search_with_pit_raw.side_effect = fake_search_with_pit_raw

    with patch("elastic_utils.search.create_client", return_value=mock_client):
        result = runner.invoke(
            cli,
            [
                "search",
                "export",
                "--index",
                "source-index",
                "--query-file",
                str(query_file),
                "--output",
                str(output_file),
                "--no-adaptive-page-size",
                "--page-size",
                "8000",
            ],
        )

    assert result.exit_code == 0
    assert "Export complete" in result.output
    assert first_request_size == 8000


def test_search_export_fails_on_preflight_shard_failures_by_default(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Export should fail by default when preflight has failed shards."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query":{"match_all":{}}}')

    shards = MagicMock()
    shards.total = 10
    shards.successful = 9
    shards.skipped = 0
    shards.failed = 1
    shards.failures = [
        {
            "index": "logs-0001",
            "shard": 1,
            "node": "node-a",
            "reason": {"type": "i_o_exception", "reason": "cache read failed"},
        }
    ]

    poll_result = MagicMock()
    poll_result.is_running = False
    poll_result.response.shards = shards
    poll_result.total_hits = 100

    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None
    mock_client.async_search_submit.return_value.id = "search-id"
    mock_client.async_search_poll.side_effect = [poll_result]

    with patch("elastic_utils.search.create_client", return_value=mock_client):
        result = runner.invoke(
            cli,
            [
                "search",
                "export",
                "--index",
                "source-index",
                "--query-file",
                str(query_file),
            ],
        )

    assert result.exit_code == 1
    assert "preflight shard failures detected" in result.output
    mock_client.primary_shard_count.assert_not_called()


def test_search_export_allows_preflight_shard_failures_when_requested(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Export should continue when --allow-shard-failures is set."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query":{"match_all":{}}}')
    output_file = tmp_path / "output.jsonl"

    shards = MagicMock()
    shards.total = 10
    shards.successful = 9
    shards.skipped = 0
    shards.failed = 1
    shards.failures = [
        {
            "index": "logs-0001",
            "shard": 1,
            "node": "node-a",
            "reason": {"type": "i_o_exception", "reason": "cache read failed"},
        }
    ]

    poll_result = MagicMock()
    poll_result.is_running = False
    poll_result.response.shards = shards
    poll_result.total_hits = 1

    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None
    mock_client.async_search_submit.return_value.id = "search-id"
    mock_client.async_search_poll.side_effect = [poll_result]
    mock_client.primary_shard_count.return_value = 1
    mock_client.search_with_pit_raw.side_effect = [
        {
            "hits": {
                "hits": [{"_id": "1", "_source": {"message": "test"}, "sort": [1]}]
            },
            "pit_id": "pit-2",
        },
        {"hits": {"hits": []}},
    ]

    with patch("elastic_utils.search.create_client", return_value=mock_client):
        result = runner.invoke(
            cli,
            [
                "search",
                "export",
                "--index",
                "source-index",
                "--query-file",
                str(query_file),
                "--output",
                str(output_file),
                "--allow-shard-failures",
            ],
        )

    assert result.exit_code == 0
    assert "Export complete" in result.output
    assert output_file.exists()


def test_search_export_interrupt_preflight_cancels_async_search(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Ctrl-C during preflight wait should cancel async search when chosen."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query":{"match_all":{}}}')

    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None
    mock_client.async_search_submit.return_value.id = "search-id"
    mock_client.async_search_poll.side_effect = KeyboardInterrupt()

    with (
        patch("elastic_utils.search.create_client", return_value=mock_client),
        patch(
            "elastic_utils.search.should_cancel_async_search_on_interrupt",
            return_value=True,
        ),
    ):
        result = runner.invoke(
            cli,
            [
                "search",
                "export",
                "--index",
                "source-index",
                "--query-file",
                str(query_file),
            ],
        )

    assert result.exit_code == 130
    mock_client.async_search_delete.assert_called_once_with("search-id", silent=True)
    assert "canceled async search" in result.output


def test_search_export_interrupt_preflight_keeps_async_search_when_declined(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """Ctrl-C during preflight wait may leave async search running by choice."""
    query_file = tmp_path / "query.json"
    query_file.write_text('{"query":{"match_all":{}}}')

    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_client
    mock_client.session.return_value.__exit__.return_value = None
    mock_client.async_search_submit.return_value.id = "search-id"
    mock_client.async_search_poll.side_effect = KeyboardInterrupt()

    with (
        patch("elastic_utils.search.create_client", return_value=mock_client),
        patch(
            "elastic_utils.search.should_cancel_async_search_on_interrupt",
            return_value=False,
        ),
    ):
        result = runner.invoke(
            cli,
            [
                "search",
                "export",
                "--index",
                "source-index",
                "--query-file",
                str(query_file),
            ],
        )

    assert result.exit_code == 130
    mock_client.async_search_delete.assert_not_called()
    assert "leaving async search running" in result.output


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


def test_search_count_integration(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
    tmp_path: Path,
) -> None:
    """Test count command against real Elasticsearch."""
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

    import httpx as real_httpx

    index_name = "count-test-index"
    try:
        real_httpx.delete(
            f"{url}/{index_name}",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            timeout=30.0,
        )
    except Exception:
        pass

    real_httpx.put(
        f"{url}/{index_name}",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={"mappings": {"properties": {"message": {"type": "text"}}}},
        timeout=30.0,
    )

    for i in range(3):
        real_httpx.post(
            f"{url}/{index_name}/_doc",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            json={"message": f"count document {i}"},
            timeout=30.0,
        )

    real_httpx.post(
        f"{url}/{index_name}/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

    # Count all docs (no query file)
    result = runner.invoke(
        cli,
        ["search", "count", "--index", index_name],
    )
    assert result.exit_code == 0
    assert "3" in result.output

    # Count with query file
    query_file = tmp_path / "count-query.json"
    query_file.write_text('{"query": {"match": {"message": "count document 0"}}}')
    result = runner.invoke(
        cli,
        [
            "search",
            "count",
            "--index",
            index_name,
            "--query-file",
            str(query_file),
        ],
    )
    assert result.exit_code == 0
    assert "1" in result.output


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


def test_search_export_import_roundtrip_integration(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
    tmp_path: Path,
) -> None:
    """Test offline-style export/import roundtrip against real Elasticsearch."""
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

    import httpx as real_httpx

    source_index = "transfer-source-index"
    dest_index = "transfer-dest-index"

    for index_name in [source_index, dest_index]:
        try:
            real_httpx.delete(
                f"{url}/{index_name}",
                auth=(
                    elasticsearch_secure_service.user,
                    elasticsearch_secure_service.password,
                ),
                timeout=30.0,
            )
        except Exception:
            pass

    mappings = {
        "mappings": {
            "properties": {
                "message": {"type": "text"},
                "@timestamp": {"type": "date"},
            }
        }
    }

    real_httpx.put(
        f"{url}/{source_index}",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json=mappings,
        timeout=30.0,
    )
    real_httpx.put(
        f"{url}/{dest_index}",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json=mappings,
        timeout=30.0,
    )

    expected_ids = ["doc-1", "doc-2", "doc-3"]
    for i, doc_id in enumerate(expected_ids, start=1):
        real_httpx.put(
            f"{url}/{source_index}/_doc/{doc_id}",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            json={
                "message": f"roundtrip message {i}",
                "@timestamp": f"2026-01-19T12:00:0{i}Z",
            },
            timeout=30.0,
        )

    real_httpx.post(
        f"{url}/{source_index}/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )
    real_httpx.post(
        f"{url}/{dest_index}/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

    query_file = tmp_path / "roundtrip-query.json"
    query_file.write_text('{"query": {"match_all": {}}}')
    output_file = tmp_path / "roundtrip-export.jsonl"

    export_result = runner.invoke(
        cli,
        [
            "search",
            "export",
            "--index",
            source_index,
            "--query-file",
            str(query_file),
            "--output",
            str(output_file),
            "--page-size",
            "2",
        ],
    )
    assert export_result.exit_code == 0
    assert output_file.exists()

    import_result = runner.invoke(
        cli,
        [
            "search",
            "import",
            "--index",
            dest_index,
            "--input",
            str(output_file),
            "--url",
            url,
            "--username",
            elasticsearch_secure_service.user,
            "--password",
            elasticsearch_secure_service.password,
            "--batch-size",
            "2",
            "--refresh",
            "wait_for",
        ],
    )
    assert import_result.exit_code == 0
    assert "Created: 3" in import_result.output
    assert "Conflicts skipped: 0" in import_result.output

    search_response = real_httpx.post(
        f"{url}/{dest_index}/_search",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={"size": 10, "query": {"match_all": {}}},
        timeout=30.0,
    )
    search_response.raise_for_status()
    hits = search_response.json()["hits"]["hits"]
    restored_ids = sorted(hit["_id"] for hit in hits)
    assert restored_ids == expected_ids

    reimport_result = runner.invoke(
        cli,
        [
            "search",
            "import",
            "--index",
            dest_index,
            "--input",
            str(output_file),
            "--url",
            url,
            "--username",
            elasticsearch_secure_service.user,
            "--password",
            elasticsearch_secure_service.password,
            "--batch-size",
            "2",
            "--refresh",
            "wait_for",
        ],
    )
    assert reimport_result.exit_code == 0
    assert "Created: 0" in reimport_result.output
    assert "Conflicts skipped: 3" in reimport_result.output


def test_search_export_import_parquet_roundtrip_integration(
    runner: CliRunner,
    mock_creds_path: Path,
    elasticsearch_secure_service: ElasticsearchSecureService,
    tmp_path: Path,
) -> None:
    """Test parquet export/import roundtrip against real Elasticsearch."""
    pytest.importorskip("pyarrow")

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

    import httpx as real_httpx

    source_index = "transfer-source-parquet-index"
    dest_index = "transfer-dest-parquet-index"

    for index_name in [source_index, dest_index]:
        try:
            real_httpx.delete(
                f"{url}/{index_name}",
                auth=(
                    elasticsearch_secure_service.user,
                    elasticsearch_secure_service.password,
                ),
                timeout=30.0,
            )
        except Exception:
            pass

    mappings = {
        "mappings": {
            "properties": {
                "message": {"type": "text"},
                "@timestamp": {"type": "date"},
            }
        }
    }

    real_httpx.put(
        f"{url}/{source_index}",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json=mappings,
        timeout=30.0,
    )
    real_httpx.put(
        f"{url}/{dest_index}",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json=mappings,
        timeout=30.0,
    )

    expected_ids = ["doc-1", "doc-2", "doc-3"]
    for i, doc_id in enumerate(expected_ids, start=1):
        real_httpx.put(
            f"{url}/{source_index}/_doc/{doc_id}",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            json={
                "message": f"parquet roundtrip message {i}",
                "@timestamp": f"2026-01-19T12:00:0{i}Z",
            },
            timeout=30.0,
        )

    real_httpx.post(
        f"{url}/{source_index}/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )
    real_httpx.post(
        f"{url}/{dest_index}/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

    query_file = tmp_path / "parquet-roundtrip-query.json"
    query_file.write_text('{"query": {"match_all": {}}}')
    output_file = tmp_path / "roundtrip-export.parquet"

    export_result = runner.invoke(
        cli,
        [
            "search",
            "export",
            "--index",
            source_index,
            "--query-file",
            str(query_file),
            "--output",
            str(output_file),
            "--format",
            "parquet",
            "--workers",
            "2",
            "--page-size",
            "2",
        ],
    )
    assert export_result.exit_code == 0
    assert output_file.exists()

    import_result = runner.invoke(
        cli,
        [
            "search",
            "import",
            "--index",
            dest_index,
            "--input",
            str(output_file),
            "--input-format",
            "parquet",
            "--url",
            url,
            "--username",
            elasticsearch_secure_service.user,
            "--password",
            elasticsearch_secure_service.password,
            "--batch-size",
            "2",
            "--refresh",
            "wait_for",
        ],
    )
    assert import_result.exit_code == 0
    assert "Created: 3" in import_result.output
    assert "Conflicts skipped: 0" in import_result.output

    search_response = real_httpx.post(
        f"{url}/{dest_index}/_search",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        json={"size": 10, "query": {"match_all": {}}},
        timeout=30.0,
    )
    search_response.raise_for_status()
    hits = search_response.json()["hits"]["hits"]
    restored_ids = sorted(hit["_id"] for hit in hits)
    assert restored_ids == expected_ids


def test_search_export_interrupt_shutdown_integration(
    runner: CliRunner,
    elasticsearch_secure_service: ElasticsearchSecureService,
    tmp_path: Path,
) -> None:
    """Ctrl-C during export should stop without thread-shutdown traceback."""
    import httpx as real_httpx

    url = (
        f"{elasticsearch_secure_service.scheme}://"
        f"{elasticsearch_secure_service.host}:{elasticsearch_secure_service.port}"
    )
    index_name = "interrupt-export-index"

    try:
        real_httpx.delete(
            f"{url}/{index_name}",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            timeout=30.0,
        )
    except Exception:
        pass

    real_httpx.put(
        f"{url}/{index_name}",
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
    for i in range(5000):
        real_httpx.post(
            f"{url}/{index_name}/_doc",
            auth=(
                elasticsearch_secure_service.user,
                elasticsearch_secure_service.password,
            ),
            json={
                "message": f"interrupt document {i}",
                "@timestamp": f"2026-01-19T12:{i // 60:02d}:{i % 60:02d}Z",
            },
            timeout=30.0,
        )
    real_httpx.post(
        f"{url}/{index_name}/_refresh",
        auth=(elasticsearch_secure_service.user, elasticsearch_secure_service.password),
        timeout=30.0,
    )

    query_file = tmp_path / "interrupt-query.json"
    query_file.write_text('{"query": {"match_all": {}}}')
    output_file = tmp_path / "interrupt-export.jsonl"

    cli_bin = Path(sys.executable).with_name("elastic-utils")
    process = subprocess.Popen(
        [  # noqa: S603
            str(cli_bin),
            "search",
            "export",
            "--index",
            index_name,
            "--query-file",
            str(query_file),
            "--output",
            str(output_file),
            "--format",
            "jsonl",
            "--workers",
            "2",
            "--no-adaptive-page-size",
            "--page-size",
            "1",
            "--url",
            url,
            "--username",
            elasticsearch_secure_service.user,
            "--password",
            elasticsearch_secure_service.password,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    output_parts: list[str] = []
    start = time.monotonic()
    while time.monotonic() - start < 120:
        if process.stdout is None:
            break
        line = process.stdout.readline()
        if line:
            output_parts.append(line)
            if "Fetching with" in line:
                break
            continue
        if process.poll() is not None:
            break

    if process.poll() is None:
        process.send_signal(signal.SIGINT)

    try:
        remaining, _ = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        remaining, _ = process.communicate(timeout=5)

    output = "".join(output_parts) + (remaining or "")

    assert process.returncode != 0
    assert "Exception ignored on threading shutdown" not in output
    assert "Interrupt received, stopping workers" in output or "Aborted." in output

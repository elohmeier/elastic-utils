"""Unit tests for jsonl module."""

import csv
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from elastic_utils.cli import cli
from elastic_utils.jsonl import get_nested, parse_field_spec


class TestGetNested:
    """Tests for get_nested helper."""

    def test_simple_path(self) -> None:
        obj = {"foo": "bar"}
        assert get_nested(obj, "foo") == "bar"

    def test_nested_path(self) -> None:
        obj = {"_source": {"host": {"name": "server1"}}}
        assert get_nested(obj, "_source.host.name") == "server1"

    def test_missing_path(self) -> None:
        obj = {"foo": "bar"}
        assert get_nested(obj, "missing") is None

    def test_partial_missing_path(self) -> None:
        obj = {"_source": {"host": {}}}
        assert get_nested(obj, "_source.host.name") is None

    def test_non_dict_intermediate(self) -> None:
        obj = {"foo": "bar"}
        assert get_nested(obj, "foo.baz") is None

    def test_none_value(self) -> None:
        obj = {"foo": None}
        assert get_nested(obj, "foo") is None

    def test_numeric_value(self) -> None:
        obj = {"count": 42}
        assert get_nested(obj, "count") == "42"


class TestParseFieldSpec:
    """Tests for parse_field_spec helper."""

    def test_with_name(self) -> None:
        assert parse_field_spec("_source.@timestamp:timestamp") == (
            "_source.@timestamp",
            "timestamp",
        )

    def test_without_name(self) -> None:
        assert parse_field_spec("_source.host.name") == (
            "_source.host.name",
            "name",
        )

    def test_simple_path(self) -> None:
        assert parse_field_spec("message") == ("message", "message")

    def test_multiple_colons(self) -> None:
        # Only split on last colon
        assert parse_field_spec("a:b:c") == ("a:b", "c")


class TestExtractCommand:
    """Tests for jsonl extract command."""

    @pytest.fixture
    def sample_jsonl(self, tmp_path: Path) -> Path:
        """Create a sample JSONL file for testing."""
        data = [
            {
                "_source": {
                    "message": "Error: code ID-0815-ABCD-1234 failed",
                    "@timestamp": "2026-01-14T07:34:50.697Z",
                    "host": {"name": "server1"},
                }
            },
            {
                "_source": {
                    "message": "Code ID-0815-WXYZ-5678 succeeded",
                    "@timestamp": "2026-01-14T08:00:00.000Z",
                    "host": {"name": "server2"},
                }
            },
            {
                "_source": {
                    "message": "No codes here",
                    "@timestamp": "2026-01-14T09:00:00.000Z",
                    "host": {"name": "server3"},
                }
            },
            # Duplicate entry to test deduplication
            {
                "_source": {
                    "message": "Error: code ID-0815-ABCD-1234 failed",
                    "@timestamp": "2026-01-14T07:34:50.697Z",
                    "host": {"name": "server1"},
                }
            },
        ]
        jsonl_file = tmp_path / "test.jsonl"
        with jsonl_file.open("w") as f:
            for item in data:
                f.write(json.dumps(item) + "\n")
        return jsonl_file

    def test_extract_csv(self, sample_jsonl: Path, tmp_path: Path) -> None:
        """Test extracting to CSV format."""
        output = tmp_path / "output.csv"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "jsonl",
                "extract",
                "-p",
                r"ID-0815-[A-Z]{4}-\d{4}",
                "-f",
                "_source.@timestamp:timestamp",
                "--format",
                "csv",
                "-o",
                str(output),
                str(sample_jsonl),
            ],
        )

        assert result.exit_code == 0, result.output
        assert output.exists()

        # Read and verify CSV
        with output.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        # Should have 2 unique entries (dedupe removes duplicate)
        assert len(rows) == 2
        assert rows[0]["match"] in [
            "ID-0815-ABCD-1234",
            "ID-0815-WXYZ-5678",
        ]
        assert "timestamp" in rows[0]

    def test_extract_xlsx(self, sample_jsonl: Path, tmp_path: Path) -> None:
        """Test extracting to xlsx format."""
        pytest.importorskip("xlsxwriter")

        output = tmp_path / "output.xlsx"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "jsonl",
                "extract",
                "-p",
                r"ID-0815-[A-Z]{4}-\d{4}",
                "-f",
                "_source.@timestamp:timestamp",
                "-o",
                str(output),
                str(sample_jsonl),
            ],
        )

        assert result.exit_code == 0, result.output
        assert output.exists()
        assert output.stat().st_size > 0

    def test_extract_no_dedupe(self, sample_jsonl: Path, tmp_path: Path) -> None:
        """Test extracting without deduplication."""
        output = tmp_path / "output.csv"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "jsonl",
                "extract",
                "-p",
                r"ID-0815-[A-Z]{4}-\d{4}",
                "--no-dedupe",
                "--format",
                "csv",
                "-o",
                str(output),
                str(sample_jsonl),
            ],
        )

        assert result.exit_code == 0, result.output

        with output.open() as f:
            reader = csv.reader(f)
            rows = list(reader)

        # Should have 3 entries (header + 2 matches + 1 duplicate)
        assert len(rows) == 4  # header + 3 data rows

    def test_extract_multiple_fields(self, sample_jsonl: Path, tmp_path: Path) -> None:
        """Test extracting with multiple fields."""
        output = tmp_path / "output.csv"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "jsonl",
                "extract",
                "-p",
                r"ID-0815-[A-Z]{4}-\d{4}",
                "-f",
                "_source.@timestamp:timestamp",
                "-f",
                "_source.host.name:host",
                "--format",
                "csv",
                "-o",
                str(output),
                str(sample_jsonl),
            ],
        )

        assert result.exit_code == 0, result.output

        with output.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert "match" in rows[0]
        assert "timestamp" in rows[0]
        assert "host" in rows[0]

    def test_extract_no_matches(self, sample_jsonl: Path, tmp_path: Path) -> None:
        """Test extracting with pattern that matches nothing."""
        output = tmp_path / "output.csv"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "jsonl",
                "extract",
                "-p",
                r"NOMATCH\d+",
                "--format",
                "csv",
                "-o",
                str(output),
                str(sample_jsonl),
            ],
        )

        assert result.exit_code == 0
        assert "No matches found" in result.output

    def test_extract_invalid_regex(self, sample_jsonl: Path, tmp_path: Path) -> None:
        """Test with invalid regex pattern."""
        output = tmp_path / "output.csv"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "jsonl",
                "extract",
                "-p",
                r"[invalid",
                "--format",
                "csv",
                "-o",
                str(output),
                str(sample_jsonl),
            ],
        )

        assert result.exit_code != 0
        assert "Invalid regex" in result.output

    def test_extract_custom_source_field(self, tmp_path: Path) -> None:
        """Test extracting from a custom source field."""
        # Create JSONL with data in different field
        data = [{"level": "ERROR", "code": "E001"}]
        jsonl_file = tmp_path / "test.jsonl"
        with jsonl_file.open("w") as f:
            f.write(json.dumps(data[0]) + "\n")

        output = tmp_path / "output.csv"
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "jsonl",
                "extract",
                "-p",
                r"E\d+",
                "-s",
                "code",
                "--format",
                "csv",
                "-o",
                str(output),
                str(jsonl_file),
            ],
        )

        assert result.exit_code == 0, result.output

        with output.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 1
        assert rows[0]["match"] == "E001"

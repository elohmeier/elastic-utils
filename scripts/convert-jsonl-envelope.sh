#!/usr/bin/env bash
# Convert old hit-envelope JSONL (with _index, _id, _source wrapper) to
# source-only JSONL.
#
# Supports plain .jsonl and zstd-compressed .jsonl.zst files.
# Requires: jq, zstd (for compressed files)
#
# Usage:
#   ./scripts/convert-jsonl-envelope.sh input.jsonl.zst output.jsonl.zst
#   ./scripts/convert-jsonl-envelope.sh input.jsonl output.jsonl
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage: $0 <input> <output>" >&2
  exit 1
fi

input="$1"
output="$2"

if [ ! -f "$input" ]; then
  echo "Error: input file not found: $input" >&2
  exit 1
fi

read_cmd="cat"
if [[ "$input" == *.zst ]]; then
  read_cmd="zstdcat"
fi

if [[ "$output" == *.zst ]]; then
  $read_cmd "$input" | jq -c '._source // .' | zstd -3 -o "$output"
else
  $read_cmd "$input" | jq -c '._source // .' > "$output"
fi

echo "Converted $input -> $output"

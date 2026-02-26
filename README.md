# elastic-utils

CLI utilities for Elasticsearch.

## Installation

```bash
uv tool install git+https://github.com/elohmeier/elastic-utils
```

Or run directly with uvx:

```bash
uvx git+https://github.com/elohmeier/elastic-utils <command>
```

## Authentication

```bash
elastic-utils auth login   # Authenticate and store API key
elastic-utils auth status  # Show authentication status
elastic-utils auth logout  # Remove stored credentials

# Self-signed/local TLS endpoints
elastic-utils auth login --url https://localhost:9200 --username elastic --password secret --insecure

# Preferred: verify with a custom CA bundle
elastic-utils auth login --url https://localhost:9200 --username elastic --password secret --ca-cert /path/to/ca.pem
```

## Async Search Commands

Run long-running searches on frozen/large indices.

### Submit an async search

```bash
# From query file
elastic-utils search submit --index alias-frozen --query-file query.json

# From stdin
cat query.json | elastic-utils search submit --index alias-frozen

# With custom options
elastic-utils search submit --index alias-frozen --query-file query.json --keep-alive 2h --wait-for 5s
```

### Check search status

```bash
elastic-utils search status <search-id>
elastic-utils search status <search-id> --wait-for 5s  # Wait up to 5s for completion
```

### Wait for completion

```bash
elastic-utils search wait <search-id>                  # Poll every 5s until complete
elastic-utils search wait <search-id> --interval 10   # Poll every 10s
elastic-utils search wait <search-id> --timeout 300   # Timeout after 5 minutes
```

### Debug shard failures

```bash
elastic-utils search debug-shards <search-id>
elastic-utils search debug-shards <search-id> --wait-for 5s
```

### Get results

```bash
elastic-utils search get <search-id>                   # Output JSONL to stdout
elastic-utils search get <search-id> -o results.jsonl  # Output to file
elastic-utils search get <search-id> --format json     # Output as JSON array
```

### Delete search

```bash
elastic-utils search delete <search-id>
```

### Export all results

Full export using async search + PIT pagination (handles frozen indices):

```bash
# Export all matching documents
elastic-utils search export --index alias-frozen --query-file query.json -o results.jsonl

# With time range (gentler on frozen indices)
elastic-utils search export --index alias-frozen --query-file query.json \
  --from-date 2025-01-01 --to-date 2025-02-01 -o jan-results.jsonl

# Custom page size
elastic-utils search export --index alias-frozen --query-file query.json \
  --page-size 500 --keep-alive 15m -o results.jsonl

# Parallel sliced export with adaptive paging
elastic-utils search export --index alias-frozen --query-file query.json \
  --workers 4 --adaptive-page-size --min-page-size 500 --max-page-size 4000 \
  -o results.jsonl

# Streaming parquet export (better compression)
elastic-utils search export --index alias-frozen --query-file query.json \
  --format parquet --parquet-compression zstd -o results.parquet

# Export with explicit source credentials (instead of stored auth)
elastic-utils search export --index alias-frozen --query-file query.json \
  --url http://source-es:9200 --username elastic --password secret \
  -o results.jsonl
```

### Import exported results

Import raw-hit JSONL created by `search export` into an existing destination index:

```bash
elastic-utils search import --index logs-restored --input results.jsonl \
  --url http://dest-es:9200 --username elastic --password secret

# API key auth
elastic-utils search import --index logs-restored --input results.jsonl \
  --url http://dest-es:9200 --api-key-id <id> --api-key <key>

# Import parquet raw-hit export
elastic-utils search import --index logs-restored --input results.parquet \
  --input-format parquet \
  --url http://dest-es:9200 --username elastic --password secret
```

Import behavior:
- Preserves source `_id`
- Uses bulk `create` operations
- Skips duplicates as conflicts (does not overwrite existing documents)
- Fails if destination index does not exist

### Offline Copy Between Clusters

For air-gapped/disconnected clusters:

```bash
# 1) Export from source cluster
elastic-utils search export --index logs-source --query-file query.json \
  --url http://source-es:9200 --username elastic --password source-pass \
  -o logs-export.jsonl

# 2) Transfer logs-export.jsonl to destination environment (scp/usb/etc.)

# 3) Import into destination cluster (index must already exist)
elastic-utils search import --index logs-destination --input logs-export.jsonl \
  --url http://dest-es:9200 --username elastic --password dest-pass
```

### Query file format

```json
{
  "size": 1000,
  "sort": [
    { "@timestamp": "desc" },
    { "_shard_doc": "desc" }
  ],
  "track_total_hits": false,
  "_source": ["@timestamp", "message", "host.name"],
  "query": {
    "bool": {
      "filter": [
        { "term": { "host.name": "example.server.com" } }
      ]
    }
  }
}
```

## Diagnostic Commands

Kubectl-style commands for inspecting Elasticsearch resources.

### Cluster version

```bash
elastic-utils version          # Show cluster version
elastic-utils version -o json  # Output as JSON
```

### List indices

```bash
elastic-utils get indices                    # List all indices
elastic-utils get indices 'logs-*'           # Filter by pattern
elastic-utils get indices '*frozen*'         # Find frozen indices
elastic-utils get indices -o wide            # Show additional columns (shards, replicas)
elastic-utils get indices -o json            # Output as JSON
elastic-utils get indices --sort docs.count  # Sort by document count
```

### List aliases

```bash
elastic-utils get aliases             # List all aliases
elastic-utils get aliases 'logs-*'    # Filter by pattern
elastic-utils get aliases -o json     # Output as JSON
```

### Describe index

Shows detailed information including settings, ILM status, and date range.

```bash
elastic-utils describe index my-index
elastic-utils describe index my-index -o json
elastic-utils describe index my-index --timestamp-field event.timestamp
```

### Describe alias

Shows member indices, date range across all indices, and ILM status.

```bash
elastic-utils describe alias logs-frozen
elastic-utils describe alias logs-frozen -o json
```

## JSONL Utilities

Post-process JSONL files exported from search commands.

### Extract patterns to Excel/CSV

Extract regex matches from JSONL files along with arbitrary fields:

```bash
# Extract ID codes with timestamp to Excel
elastic-utils jsonl extract \
  --pattern 'ID-\d{4}-[A-Z]+' \
  --field '_source.@timestamp:timestamp' \
  -o output.xlsx \
  search-results.jsonl

# Extract to CSV format
elastic-utils jsonl extract \
  --pattern 'ERROR|WARN' \
  --source-field '_source.level' \
  --format csv \
  -o output.csv \
  logs.jsonl

# Include multiple fields
elastic-utils jsonl extract \
  --pattern 'ID-\d{4}-[A-Z]+' \
  --field '_source.@timestamp:timestamp' \
  --field '_source.host.name:host' \
  --field '_source.log.level:level' \
  -o output.xlsx \
  search-results.jsonl

# Disable deduplication
elastic-utils jsonl extract \
  --pattern 'user=(\w+)' \
  --no-dedupe \
  --format csv \
  -o users.csv \
  access.jsonl
```

**Note:** Excel output requires xlsxwriter. Install with:

```bash
uv tool install 'git+https://github.com/elohmeier/elastic-utils[xlsx]'
```

Parquet export/import is supported by default (pyarrow is installed as a core dependency).

## Development

```bash
# Run tests (requires Docker)
uv run pytest tests/

# Run tests without Docker (skips ES integration tests)
uv run pytest tests/ -k 'not elasticsearch_secure'
```

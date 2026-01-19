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

## Development

```bash
# Run tests (requires Docker)
uv run pytest tests/

# Run tests without Docker (skips ES integration tests)
uv run pytest tests/ -k 'not elasticsearch_secure'
```

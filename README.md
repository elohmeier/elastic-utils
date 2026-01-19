# elastic-utils

CLI utilities for Elasticsearch.

## Usage

Run directly with uvx:

```bash
uvx git+https://github.com/elohmeier/elastic-utils auth login
```

Or install with uv/pip:

```bash
uv tool install git+https://github.com/elohmeier/elastic-utils
elastic-utils auth login
elastic-utils auth status
elastic-utils auth logout
```

## Development

```bash
# Run tests (requires Docker)
uv run pytest tests/

# Run tests without Docker (skips ES integration tests)
uv run pytest tests/ -k 'not elasticsearch_secure'
```

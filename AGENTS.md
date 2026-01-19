# Agent Instructions

## Project Structure

```
src/elastic_utils/
├── __init__.py      # Entry point, exports main()
├── cli.py           # Click CLI groups
├── auth.py          # Auth commands (login/logout/status)
└── config.py        # Credential storage (XDG data dir)

tests/
├── conftest.py      # Elasticsearch fixture with security enabled
├── test_auth.py     # Auth command tests (uses real ES via Docker)
└── test_config.py   # Config unit tests
```

## Commands

```bash
uv sync                   # Install dependencies
uv run pytest             # Run tests (requires Docker)
uv run elastic-utils      # Run CLI
ruff format .             # Format code
ruff check --fix .        # Lint and fix
```

## Testing

Tests use `pytest-databases` to spin up Elasticsearch in Docker. The custom fixture in `conftest.py` enables security (`xpack.security.enabled=true`) for API key testing.

## Credentials

Stored at `~/.local/share/elastic-utils/credentials.json` via `platformdirs`.

## Adding New Commands

After adding new CLI commands:
1. Update `README.md` with usage examples
2. Update this file if the project structure changes

# Agent Instructions

## Project Structure

```
src/elastic_utils/
├── __init__.py      # Entry point, exports main()
├── cli.py           # Click CLI groups
├── auth.py          # Auth commands (login/logout/status)
├── search.py        # Async search commands (submit/status/wait/get/delete/export)
├── get.py           # List commands (get indices/aliases)
├── describe.py      # Detail commands (describe index/alias)
├── version.py       # Cluster version command
├── client.py        # ElasticsearchClient class with error handling
├── models.py        # Pydantic response models
├── formatting.py    # Output formatting utilities
└── config.py        # Credential storage (XDG data dir)

tests/
├── conftest.py      # Elasticsearch fixture with security enabled
├── test_auth.py     # Auth command tests (uses real ES via Docker)
├── test_search.py   # Search command tests
├── test_get.py      # Get command tests
├── test_describe.py # Describe command tests
├── test_version.py  # Version command tests
└── test_config.py   # Config unit tests
```

## Commands

```bash
uv sync                   # Install dependencies
uv run pytest             # Run tests (requires Docker)
uv run elastic-utils      # Run CLI
ruff format .             # Format code
ruff check --fix .        # Lint and fix
ty check .                # Type check
```

## Testing

Tests use `pytest-databases` to spin up Elasticsearch in Docker. The custom fixture in `conftest.py` enables security (`xpack.security.enabled=true`) for API key testing.

## Credentials

Stored at `~/.local/share/elastic-utils/credentials.json` via `platformdirs`.

## Pydantic Models

All API response models in `models.py` are validated against the official Elasticsearch specification:

```
~/repos/github.com/elastic/elasticsearch-specification/
```

### Specification Structure

```
specification/
├── _types/               # Shared types (Stats.ts, Base.ts, common.ts)
├── _global/              # Global APIs (search, info, open_point_in_time)
├── async_search/         # Async search endpoints
├── cat/                  # CAT APIs (indices, aliases)
├── security/             # Security APIs (create_api_key)
└── ilm/                  # ILM APIs (explain_lifecycle)
```

### Key Spec Files

| Model                 | Spec File                                                 |
| --------------------- | --------------------------------------------------------- |
| `Shards`              | `_types/Stats.ts` → `ShardStatistics`                     |
| `TotalHits`           | `_global/search/_types/hits.ts` → `TotalHits`             |
| `HitsContainer`       | `_global/search/_types/hits.ts` → `HitsMetadata`          |
| `AsyncSearchResponse` | `async_search/_types/AsyncSearchResponseBase.ts`          |
| `SearchResponse`      | `_global/search/SearchResponse.ts` → `ResponseBody`       |
| `PITResponse`         | `_global/open_point_in_time/OpenPointInTimeResponse.ts`   |
| `IndexInfo`           | `cat/indices/types.ts` → `IndicesRecord`                  |
| `AliasInfo`           | `cat/aliases/types.ts` → `AliasesRecord`                  |
| `ApiKeyResponse`      | `security/create_api_key/SecurityCreateApiKeyResponse.ts` |
| `ClusterInfo`         | `_global/info/RootNodeInfoResponse.ts`                    |
| `ClusterVersion`      | `_types/Base.ts` → `ElasticsearchVersionInfo`             |
| `ILMIndexInfo`        | `ilm/explain_lifecycle/types.ts` → `LifecycleExplain`     |

### Model Guidelines

1. **Required vs Optional**: Follow the spec exactly. Fields marked with `?` in TypeScript are optional
2. **Types**: Use spec types (`str` for strings, `int` for integers). Note: `_cat` APIs return all values as strings
3. **Aliases**: Use `Field(alias="...")` for fields with dots (e.g., `docs.count` → `docs_count`)
4. **Defaults**: Only use defaults for truly optional fields; required fields should have no default

### Adding/Modifying Models

1. Find the relevant spec file in `elasticsearch-specification/specification/`
2. Check required vs optional fields (look for `?` suffix)
3. Match types exactly (note: TypeScript `long` → Python `int`)
4. Add docstring with spec file reference
5. Update tests with complete mock data for required fields

## Adding New Commands

After adding new CLI commands:

1. Update `README.md` with usage examples
2. Update this file if the project structure changes

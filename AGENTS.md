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

## Commit Convention

This project uses [Conventional Commits](https://www.conventionalcommits.org/). Commit messages must follow the format:

```
<type>(<scope>): <description>
```

Common types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`, `perf`.

### Release implications

Releases are automated via [semantic-release](https://github.com/semantic-release/semantic-release) on every push to `main` (see `.releaserc.json` and `.github/workflows/release.yml`):

- `fix:` commits trigger a **patch** release (e.g., 1.0.0 → 1.0.1)
- `feat:` commits trigger a **minor** release (e.g., 1.0.0 → 1.1.0)
- `BREAKING CHANGE:` in the commit footer (or `!` after the type) triggers a **major** release (e.g., 1.0.0 → 2.0.0)
- Other types (`docs`, `chore`, `refactor`, `test`, `ci`) do **not** trigger a release

When a release is triggered, semantic-release:

1. Bumps the version in `pyproject.toml`, `snapshot-query-cli/build.gradle`, and `uv.lock`
2. Commits the version bump with `chore(release): <version> [skip ci]`
3. Creates a GitHub release with auto-generated release notes
4. Builds and attaches the `snapshot-query-cli` shadow JAR to the GitHub release

## snapshot-query-cli (Java)

A standalone CLI (shadow JAR) that queries Elasticsearch snapshots offline directly from S3-compatible storage, without a running Elasticsearch cluster.

### Project Structure

```
snapshot-query-cli/
├── build.gradle                    # Gradle build with shadowJar plugin
├── settings.gradle
├── src/main/java/org/elasticsearch/snapshotquery/
│   ├── SnapshotQueryMain.java      # Entry point, picocli multi-command
│   ├── SnapshotQueryCli.java       # "query" subcommand
│   ├── SnapshotExportCli.java      # "export" subcommand (JSONL export)
│   ├── ExportRangeCli.java         # "export-range" subcommand (multi-index date range)
│   ├── ListSnapshotsCli.java       # "snapshots" subcommand
│   ├── SnapshotQueryCliProvider.java # Shared logic for opening snapshot indices
│   ├── SnapshotQueryDirectory.java # Lucene Directory backed by S3 snapshot blobs
│   ├── SnapshotMetadataLoader.java # Loads snapshot/index metadata from S3
│   ├── SnapshotExportSupport.java  # Shared export utilities (JSONL, zstd)
│   ├── SearchBodyParser.java       # Parses full ES search body JSON
│   ├── SimpleQueryTranslator.java  # Translates ES query DSL → Lucene queries
│   ├── S3Options.java              # picocli S3 connection options mixin
│   ├── S3ClientFactory.java        # AWS S3 client setup
│   ├── ProfilingRecorder.java      # Records performance counters
│   └── ProgressReporter.java       # Progress bar for exports
└── tests/
    └── integration-test.sh         # End-to-end test (MinIO + ES via Docker)
```

### Commands

```bash
cd snapshot-query-cli
./gradlew shadowJar                # Build standalone JAR
./gradlew clean shadowJar          # Clean build

# Run integration tests (requires Docker, Java 21+, jq, curl)
./tests/integration-test.sh
./tests/integration-test.sh --skip-build          # Skip JAR rebuild
./tests/integration-test.sh --jar path/to/cli.jar # Use specific JAR
```

### Integration Tests

The integration test (`tests/integration-test.sh`) is a comprehensive end-to-end test that:

1. Starts MinIO (S3-compatible) and Elasticsearch via Docker
2. Indexes test documents and creates snapshots to MinIO
3. Stops Elasticsearch
4. Runs query/export/snapshots CLI commands against the offline snapshot
5. Validates results (hit counts, JSONL output, zstd compression, alias resolution, date range filtering, export-range with profiling)

### Elasticsearch Reference Code

The snapshot-query-cli reads Elasticsearch snapshot formats directly. The upstream Elasticsearch source at `~/repos/github.com/elastic/elasticsearch` is useful for understanding internal formats:

```
server/src/main/java/org/elasticsearch/
├── snapshots/
│   ├── SnapshotInfo.java               # Snapshot metadata model
│   ├── SnapshotId.java                 # Snapshot identifier
│   ├── SnapshotState.java              # Snapshot states (SUCCESS, FAILED, etc.)
│   └── SnapshotsService.java           # Snapshot lifecycle management
├── repositories/
│   ├── RepositoryData.java             # Repository-level metadata (index of snapshots)
│   ├── IndexId.java                    # Index identifier within a repository
│   ├── ShardGenerations.java           # Shard generation tracking
│   └── blobstore/
│       ├── BlobStoreRepository.java    # Blob storage implementation (S3/GCS/Azure)
│       └── ChecksumBlobStoreFormat.java # Serialization format for metadata blobs
└── index/engine/
    └── Engine.java                     # Lucene index engine (searcher lifecycle)
```

## Adding New Commands

After adding new CLI commands:

1. Update `README.md` with usage examples
2. Update this file if the project structure changes

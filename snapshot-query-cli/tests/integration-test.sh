#!/usr/bin/env bash
#
# Integration test for elasticsearch-snapshot-query CLI (standalone JAR).
#
# Spins up MinIO (S3-compatible) and Elasticsearch via docker,
# indexes test documents, creates a snapshot to MinIO, stops ES,
# then queries the snapshot offline using the standalone JAR.
#
# Prerequisites:
#   - docker
#   - curl
#   - jq
#   - Java 21+
#   - zstd (for compression tests)
#
# Usage:
#   ./integration-test.sh [--skip-build] [--jar path/to/cli.jar]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Configuration ────────────────────────────────────────────────────────────

MINIO_CONTAINER="snapshot-query-minio"
MINIO_PORT=9400
MINIO_CONSOLE_PORT=9401
MINIO_ROOT_USER="minioadmin"
MINIO_ROOT_PASSWORD="minioadmin"
MINIO_BUCKET="test-snapshots"
MINIO_ENDPOINT="http://localhost:${MINIO_PORT}"

ES_CONTAINER="snapshot-query-es"
ES_HTTP_PORT=9250
ES_VERSION="9.0.3"

SNAPSHOT_REPO="test-repo"
SNAPSHOT_NAME="test-snap"
INDEX_NAME="test-logs"
ALIAS_NAME="test-logs-alias"
RANGE_INDEX_ONE=".ds-logs-180-default-2024.01.15-000001"
RANGE_INDEX_TWO=".ds-logs-180-default-2024.01.16-000002"
RANGE_INDEX_THREE=".ds-logs-180-default-2024.01.16-000003"
RANGE_SNAPSHOT_ONE="range-snap-1"
RANGE_SNAPSHOT_TWO_OLD="range-snap-2-old"
RANGE_SNAPSHOT_TWO_NEW="range-snap-2-new"
RANGE_SNAPSHOT_THREE="range-snap-3"

SKIP_BUILD=false
JAR_PATH=""
PASSED=0
FAILED=0

# ── Helpers ──────────────────────────────────────────────────────────────────

log()  { echo -e "\033[1;34m==>\033[0m $*"; }
ok()   { echo -e "\033[1;32m  PASS:\033[0m $*"; PASSED=$((PASSED + 1)); }
fail() { echo -e "\033[1;31m  FAIL:\033[0m $*"; FAILED=$((FAILED + 1)); }
die()  { echo -e "\033[1;31mERROR:\033[0m $*" >&2; cleanup; exit 1; }

wait_for_url() {
    local url="$1" max_wait="${2:-60}" interval="${3:-2}"
    local elapsed=0
    while ! curl -sf "$url" >/dev/null 2>&1; do
        sleep "$interval"
        elapsed=$((elapsed + interval))
        if [ "$elapsed" -ge "$max_wait" ]; then
            return 1
        fi
    done
}

es_api() {
    local method="$1" path="$2"
    shift 2
    curl -sf -X "$method" "http://localhost:${ES_HTTP_PORT}${path}" \
        -H 'Content-Type: application/json' "$@"
}

create_logs_index() {
    local index_name="$1"
    es_api PUT "/${index_name}" -d '{
  "settings": {
    "number_of_shards": 2,
    "number_of_replicas": 0
  },
  "mappings": {
    "properties": {
      "message":   { "type": "text" },
      "level":     { "type": "keyword" },
      "status":    { "type": "keyword" },
      "@timestamp": { "type": "date" },
      "count":     { "type": "long" },
      "host":      { "type": "keyword" }
    }
  }
}' >/dev/null
}

create_snapshot() {
    local snapshot_name="$1" indices="$2"
    es_api PUT "/_snapshot/${SNAPSHOT_REPO}/${snapshot_name}?wait_for_completion=true" -d "{
  \"indices\": \"${indices}\",
  \"include_global_state\": false
}" >/dev/null
}

# ── Cleanup ──────────────────────────────────────────────────────────────────

cleanup() {
    log "Cleaning up..."
    if docker container inspect "$ES_CONTAINER" >/dev/null 2>&1; then
        log "Stopping Elasticsearch container"
        docker rm -f "$ES_CONTAINER" >/dev/null 2>&1 || true
    fi
    if docker container inspect "$MINIO_CONTAINER" >/dev/null 2>&1; then
        log "Stopping MinIO container"
        docker rm -f "$MINIO_CONTAINER" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# ── Parse args ───────────────────────────────────────────────────────────────

for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=true ;;
        --jar)        shift; JAR_PATH="$1" ;;
        --jar=*)      JAR_PATH="${arg#--jar=}" ;;
        *)            die "Unknown argument: $arg" ;;
    esac
done

# ── Step 1: Build the standalone JAR ─────────────────────────────────────────

if [ -z "$JAR_PATH" ]; then
    JAR_PATH=$(ls -1 "$PROJECT_ROOT"/build/libs/elasticsearch-snapshot-query-cli-*.jar 2>/dev/null | head -1)
fi

if [ "$SKIP_BUILD" = false ]; then
    log "Building standalone JAR..."
    cd "$PROJECT_ROOT"
    JAVA_HOME="${JAVA_HOME:-}" ./gradlew clean shadowJar 2>&1 | tail -5
fi

[ -f "$JAR_PATH" ] || die "JAR not found at $JAR_PATH. Run without --skip-build."
log "Using JAR: $JAR_PATH"

# ── Step 2: Start MinIO ─────────────────────────────────────────────────────

log "Starting MinIO via docker..."
docker rm -f "$MINIO_CONTAINER" 2>/dev/null || true

docker run -d \
    --name "$MINIO_CONTAINER" \
    -p "${MINIO_PORT}:9000" \
    -p "${MINIO_CONSOLE_PORT}:9001" \
    -e "MINIO_ROOT_USER=${MINIO_ROOT_USER}" \
    -e "MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}" \
    docker.io/minio/minio:latest \
    server /data --console-address ":9001"

log "Waiting for MinIO to be ready..."
wait_for_url "${MINIO_ENDPOINT}/minio/health/live" 30 || die "MinIO failed to start"
log "MinIO is ready at ${MINIO_ENDPOINT}"

# Create bucket
log "Creating bucket [${MINIO_BUCKET}]..."
if command -v mc &>/dev/null; then
    mc alias set testminio "$MINIO_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" --api S3v4 >/dev/null
    mc mb "testminio/${MINIO_BUCKET}" 2>/dev/null || true
else
    docker exec "$MINIO_CONTAINER" \
        mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1
    docker exec "$MINIO_CONTAINER" \
        mc mb "local/${MINIO_BUCKET}" 2>/dev/null || true
fi

# ── Step 3: Start Elasticsearch (containerized) ─────────────────────────────

log "Starting Elasticsearch ${ES_VERSION} via docker..."
docker rm -f "$ES_CONTAINER" 2>/dev/null || true

docker run -d \
    --name "$ES_CONTAINER" \
    -p "${ES_HTTP_PORT}:9200" \
    -e "discovery.type=single-node" \
    -e "xpack.security.enabled=false" \
    -e "xpack.ml.enabled=false" \
    -e "ES_JAVA_OPTS=-Xms512m -Xmx512m" \
    -e "s3.client.default.endpoint=http://host.docker.internal:${MINIO_PORT}" \
    -e "s3.client.default.region=us-east-1" \
    -e "s3.client.default.path_style_access=true" \
    docker.elastic.co/elasticsearch/elasticsearch:${ES_VERSION}

log "Waiting for Elasticsearch to be ready..."
wait_for_url "http://localhost:${ES_HTTP_PORT}/_cluster/health" 120 || die "Elasticsearch failed to start"

# Install repository-s3 plugin and set credentials via keystore
log "Installing repository-s3 plugin..."
docker exec "$ES_CONTAINER" bash -c \
    'if ! bin/elasticsearch-plugin list | grep -q repository-s3; then
        bin/elasticsearch-plugin install --batch repository-s3
    fi'

# Set S3 credentials in keystore
docker exec "$ES_CONTAINER" bash -c \
    "echo '${MINIO_ROOT_USER}' | bin/elasticsearch-keystore add -xf s3.client.default.access_key && \
     echo '${MINIO_ROOT_PASSWORD}' | bin/elasticsearch-keystore add -xf s3.client.default.secret_key"

# Restart ES to pick up the plugin and keystore
log "Restarting Elasticsearch to load repository-s3 plugin..."
docker restart "$ES_CONTAINER"
wait_for_url "http://localhost:${ES_HTTP_PORT}/_cluster/health" 120 || die "Elasticsearch failed to restart"
log "Elasticsearch is ready"

# ── Step 4: Index test documents ─────────────────────────────────────────────

log "Creating index [${INDEX_NAME}] and indexing test documents..."

create_logs_index "$INDEX_NAME" || die "Failed to create index"

# Add alias
log "Adding alias [${ALIAS_NAME}] to index [${INDEX_NAME}]..."
es_api POST "/_aliases" -d "{
  \"actions\": [
    { \"add\": { \"index\": \"${INDEX_NAME}\", \"alias\": \"${ALIAS_NAME}\" } }
  ]
}" | jq -r '.acknowledged' || die "Failed to add alias"

es_api POST "/_bulk" -d '
{"index":{"_index":"test-logs","_id":"1"}}
{"message":"Server started successfully","level":"info","status":"ok","@timestamp":"2024-01-15T10:00:00Z","count":1,"host":"web-01"}
{"index":{"_index":"test-logs","_id":"2"}}
{"message":"Connection timeout to database","level":"error","status":"error","@timestamp":"2024-01-15T10:01:00Z","count":3,"host":"web-01"}
{"index":{"_index":"test-logs","_id":"3"}}
{"message":"Request processed in 250ms","level":"info","status":"ok","@timestamp":"2024-01-16T10:02:00Z","count":42,"host":"web-02"}
{"index":{"_index":"test-logs","_id":"4"}}
{"message":"Disk usage above 90%","level":"warn","status":"warning","@timestamp":"2024-01-16T10:03:00Z","count":1,"host":"web-03"}
{"index":{"_index":"test-logs","_id":"5"}}
{"message":"Authentication failed for user admin","level":"error","status":"error","@timestamp":"2024-01-17T10:04:00Z","count":5,"host":"web-02"}
{"index":{"_index":"test-logs","_id":"6"}}
{"message":"Cache cleared","level":"info","status":"ok","@timestamp":"2024-01-17T10:05:00Z","count":1,"host":"web-01"}
{"index":{"_index":"test-logs","_id":"7"}}
{"message":"Out of memory error","level":"error","status":"error","@timestamp":"2024-01-18T10:06:00Z","count":1,"host":"web-03"}
{"index":{"_index":"test-logs","_id":"8"}}
{"message":"Backup completed","level":"info","status":"ok","@timestamp":"2024-01-18T10:07:00Z","count":1,"host":"web-01"}
{"index":{"_index":"test-logs","_id":"9"}}
{"message":"SSL certificate expiring soon","level":"warn","status":"warning","@timestamp":"2024-01-19T10:08:00Z","count":2,"host":"web-02"}
{"index":{"_index":"test-logs","_id":"10"}}
{"message":"New deployment rolled out","level":"info","status":"ok","@timestamp":"2024-01-19T10:09:00Z","count":1,"host":"web-03"}
' | jq -r '.errors' || die "Failed to bulk index"

es_api POST "/${INDEX_NAME}/_flush" >/dev/null 2>&1 || true
es_api POST "/${INDEX_NAME}/_refresh" >/dev/null 2>&1 || true

DOC_COUNT=$(es_api GET "/${INDEX_NAME}/_count" | jq -r '.count')
log "Indexed ${DOC_COUNT} documents"

log "Creating date-stamped indices for export-range tests..."
create_logs_index "$RANGE_INDEX_ONE" || die "Failed to create ${RANGE_INDEX_ONE}"
create_logs_index "$RANGE_INDEX_TWO" || die "Failed to create ${RANGE_INDEX_TWO}"
create_logs_index "$RANGE_INDEX_THREE" || die "Failed to create ${RANGE_INDEX_THREE}"

es_api POST "/_bulk" -d "
{\"index\":{\"_index\":\"${RANGE_INDEX_ONE}\",\"_id\":\"1\"}}
{\"message\":\"range one doc one\",\"level\":\"info\",\"status\":\"ok\",\"@timestamp\":\"2024-01-15T09:00:00Z\",\"count\":1,\"host\":\"range-01\"}
{\"index\":{\"_index\":\"${RANGE_INDEX_ONE}\",\"_id\":\"2\"}}
{\"message\":\"range one doc two\",\"level\":\"error\",\"status\":\"error\",\"@timestamp\":\"2024-01-15T10:00:00Z\",\"count\":2,\"host\":\"range-01\"}
{\"index\":{\"_index\":\"${RANGE_INDEX_TWO}\",\"_id\":\"1\"}}
{\"message\":\"range two doc one\",\"level\":\"info\",\"status\":\"ok\",\"@timestamp\":\"2024-01-16T09:00:00Z\",\"count\":1,\"host\":\"range-02\"}
{\"index\":{\"_index\":\"${RANGE_INDEX_TWO}\",\"_id\":\"2\"}}
{\"message\":\"range two doc two\",\"level\":\"warn\",\"status\":\"warning\",\"@timestamp\":\"2024-01-16T10:00:00Z\",\"count\":2,\"host\":\"range-02\"}
{\"index\":{\"_index\":\"${RANGE_INDEX_THREE}\",\"_id\":\"1\"}}
{\"message\":\"range three doc one\",\"level\":\"info\",\"status\":\"ok\",\"@timestamp\":\"2024-01-16T11:00:00Z\",\"count\":1,\"host\":\"range-03\"}
{\"index\":{\"_index\":\"${RANGE_INDEX_THREE}\",\"_id\":\"2\"}}
{\"message\":\"range three doc two\",\"level\":\"error\",\"status\":\"error\",\"@timestamp\":\"2024-01-16T12:00:00Z\",\"count\":2,\"host\":\"range-03\"}
" | jq -r '.errors' || die "Failed to bulk index range test data"

es_api POST "/${RANGE_INDEX_ONE},${RANGE_INDEX_TWO},${RANGE_INDEX_THREE}/_flush" >/dev/null 2>&1 || true
es_api POST "/${RANGE_INDEX_ONE},${RANGE_INDEX_TWO},${RANGE_INDEX_THREE}/_refresh" >/dev/null 2>&1 || true

# ── Step 5: Create S3 snapshot repository and take snapshot ──────────────────

log "Registering S3 snapshot repository [${SNAPSHOT_REPO}]..."

# Reload secure settings so ES picks up the keystore entries
es_api POST "/_nodes/reload_secure_settings" -d '{}' >/dev/null 2>&1 || true

es_api PUT "/_snapshot/${SNAPSHOT_REPO}" -d "{
  \"type\": \"s3\",
  \"settings\": {
    \"bucket\": \"${MINIO_BUCKET}\",
    \"client\": \"default\",
    \"path_style_access\": true
  }
}" | jq -r '.acknowledged' || die "Failed to register repository"

log "Creating snapshot [${SNAPSHOT_NAME}]..."
es_api PUT "/_snapshot/${SNAPSHOT_REPO}/${SNAPSHOT_NAME}?wait_for_completion=true" -d "{
  \"indices\": \"${INDEX_NAME}\",
  \"include_global_state\": false
}" | jq -r '.snapshot.state' || die "Failed to create snapshot"

SNAP_STATE=$(es_api GET "/_snapshot/${SNAPSHOT_REPO}/${SNAPSHOT_NAME}" | jq -r '.snapshots[0].state')
log "Snapshot state: ${SNAP_STATE}"
[ "$SNAP_STATE" = "SUCCESS" ] || die "Snapshot failed with state: ${SNAP_STATE}"

log "Creating range snapshots for export-range tests..."
create_snapshot "$RANGE_SNAPSHOT_ONE" "$RANGE_INDEX_ONE" || die "Failed to create ${RANGE_SNAPSHOT_ONE}"
create_snapshot "$RANGE_SNAPSHOT_TWO_OLD" "$RANGE_INDEX_TWO" || die "Failed to create ${RANGE_SNAPSHOT_TWO_OLD}"
sleep 1
create_snapshot "$RANGE_SNAPSHOT_TWO_NEW" "$RANGE_INDEX_TWO" || die "Failed to create ${RANGE_SNAPSHOT_TWO_NEW}"
create_snapshot "$RANGE_SNAPSHOT_THREE" "$RANGE_INDEX_THREE" || die "Failed to create ${RANGE_SNAPSHOT_THREE}"

# ── Step 6: Stop Elasticsearch ───────────────────────────────────────────────

log "Stopping Elasticsearch..."
docker stop "$ES_CONTAINER" >/dev/null 2>&1 || true
log "Elasticsearch stopped — now querying snapshot offline"

# ── Step 7: Run CLI query tests against the snapshot ──────────────────────────

log ""
log "============================================"
log "  Running snapshot-query CLI tests"
log "============================================"
log ""

JAVA="${JAVA_HOME:+${JAVA_HOME}/bin/}java"

CLI_COMMON_ARGS=(
    --bucket "$MINIO_BUCKET"
    --endpoint "$MINIO_ENDPOINT"
    --region us-east-1
    --access-key "$MINIO_ROOT_USER"
    --secret-key "$MINIO_ROOT_PASSWORD"
    --snapshot "$SNAPSHOT_NAME"
    --index "$INDEX_NAME"
)

run_query() {
    local desc="$1" query="$2"
    shift 2
    echo -e "\033[1;34m==>\033[0m Test: ${desc}" >&2
    local raw_output json_output
    raw_output=$("$JAVA" -jar "$JAR_PATH" query \
        "${CLI_COMMON_ARGS[@]}" \
        --query "$query" \
        "$@" 2>/dev/null) || true
    json_output=$(echo "$raw_output" | awk '
        /^[{][[:space:]]*$/ { found=1; buf="" }
        found { buf = buf $0 "\n" }
        /^[}][[:space:]]*$/ && found { result = buf }
        END { printf "%s", result }
    ')
    if [ -z "$json_output" ]; then
        json_output="$raw_output"
    fi
    echo "$json_output"
}

assert_hit_count() {
    local desc="$1" expected="$2" output="$3"
    local actual
    actual=$(echo "$output" | jq -r '.hits.total // 0' 2>/dev/null || echo "parse_error")
    if [ "$actual" = "$expected" ]; then
        ok "${desc}: got ${actual} hits (expected ${expected})"
    else
        fail "${desc}: got ${actual} hits (expected ${expected})"
        echo "    Output: $(echo "$output" | head -5)"
    fi
}

assert_output_contains() {
    local desc="$1" needle="$2" output="$3"
    if echo "$output" | grep -q "$needle"; then
        ok "${desc}: output contains '${needle}'"
    else
        fail "${desc}: output missing '${needle}'"
        echo "    Output: $(echo "$output" | head -5)"
    fi
}

# ── Test 1: match_all ────────────────────────────────────────────────────────

OUTPUT=$(run_query "match_all query" '{"match_all":{}}' --size 20) || true
assert_hit_count "match_all" "10" "$OUTPUT"

# ── Test 2: term query on keyword field ──────────────────────────────────────

OUTPUT=$(run_query "term query (level=error)" '{"term":{"level":"error"}}' --size 10) || true
assert_hit_count "term(level=error)" "3" "$OUTPUT"
assert_output_contains "term(level=error) has error docs" "error" "$OUTPUT"

# ── Test 3: term query — single result ───────────────────────────────────────

OUTPUT=$(run_query "term query (host=web-03)" '{"term":{"host":"web-03"}}' --size 10) || true
assert_hit_count "term(host=web-03)" "3" "$OUTPUT"

# ── Test 4: bool query with must + must_not ──────────────────────────────────

OUTPUT=$(run_query "bool must+must_not" '{"bool":{"must":[{"term":{"level":"info"}}],"must_not":[{"term":{"host":"web-01"}}]}}' --size 10) || true
assert_hit_count "bool(must:info, must_not:web-01)" "2" "$OUTPUT"

# ── Test 5: terms query ─────────────────────────────────────────────────────

OUTPUT=$(run_query "terms query" '{"terms":{"status":["error","warning"]}}' --size 10) || true
assert_hit_count "terms(error|warning)" "5" "$OUTPUT"

# ── Test 6: exists query ─────────────────────────────────────────────────────

OUTPUT=$(run_query "exists query" '{"exists":{"field":"count"}}' --size 20) || true
assert_hit_count "exists(count)" "10" "$OUTPUT"

# ── Test 7: prefix query ────────────────────────────────────────────────────

OUTPUT=$(run_query "prefix query" '{"prefix":{"host":"web-0"}}' --size 10) || true
assert_hit_count "prefix(host=web-0*)" "10" "$OUTPUT"

# ── Test 8: wildcard query ──────────────────────────────────────────────────

OUTPUT=$(run_query "wildcard query" '{"wildcard":{"host":"*-02"}}' --size 10) || true
assert_hit_count "wildcard(host=*-02)" "3" "$OUTPUT"

# ── Test 9: size limiting ────────────────────────────────────────────────────

OUTPUT=$(run_query "size=2" '{"match_all":{}}' --size 2) || true
RETURNED=$(echo "$OUTPUT" | jq -r '.hits.hits | length' 2>/dev/null || echo "0")
if [ "$RETURNED" = "2" ]; then
    ok "size limiting: returned ${RETURNED} hits with --size 2"
else
    fail "size limiting: returned ${RETURNED} hits, expected 2"
fi

# ── Test 10: _source is present ──────────────────────────────────────────────

OUTPUT=$(run_query "_source check" '{"term":{"level":"info"}}' --size 1) || true
if echo "$OUTPUT" | jq -e '.hits.hits[0]._source.message' >/dev/null 2>&1; then
    ok "_source present: documents include _source with message field"
else
    fail "_source present: _source or message field missing"
fi

# ══════════════════════════════════════════════════════════════════════════════
# Export tests
# ══════════════════════════════════════════════════════════════════════════════

log ""
log "============================================"
log "  Running snapshot-export CLI tests"
log "============================================"
log ""

EXPORT_DIR=$(mktemp -d)
trap 'rm -rf "$EXPORT_DIR"; cleanup' EXIT

EXPORT_COMMON_ARGS=(
    --bucket "$MINIO_BUCKET"
    --endpoint "$MINIO_ENDPOINT"
    --region us-east-1
    --access-key "$MINIO_ROOT_USER"
    --secret-key "$MINIO_ROOT_PASSWORD"
    --snapshot "$SNAPSHOT_NAME"
)

run_export() {
    local desc="$1"
    shift
    echo -e "\033[1;34m==>\033[0m Export test: ${desc}" >&2
    "$JAVA" -jar "$JAR_PATH" export \
        "${EXPORT_COMMON_ARGS[@]}" \
        "$@" 2>/dev/null
}

# ── Export Test 1: basic JSONL export ─────────────────────────────────────────

OUTFILE="$EXPORT_DIR/basic-export.jsonl"
run_export "basic JSONL export (match_all)" \
    --index "$INDEX_NAME" \
    --query '{"match_all":{}}' \
    -o "$OUTFILE" || true

if [ -f "$OUTFILE" ]; then
    LINE_COUNT=$(wc -l < "$OUTFILE" | tr -d ' ')
    if [ "$LINE_COUNT" = "10" ]; then
        ok "basic export: got ${LINE_COUNT} lines (expected 10)"
    else
        fail "basic export: got ${LINE_COUNT} lines (expected 10)"
    fi
    # Verify each line is valid JSON
    INVALID_LINES=$(while IFS= read -r line; do echo "$line" | jq empty 2>&1 && true || echo "invalid"; done < "$OUTFILE" | grep -c "invalid" || true)
    if [ "$INVALID_LINES" = "0" ]; then
        ok "basic export: all lines are valid JSON"
    else
        fail "basic export: ${INVALID_LINES} lines are invalid JSON"
    fi
else
    fail "basic export: output file not created"
fi

# ── Export Test 2: date range filtering ───────────────────────────────────────

OUTFILE="$EXPORT_DIR/date-range-export.jsonl"
run_export "date range filter (2024-01-16 to 2024-01-18)" \
    --index "$INDEX_NAME" \
    --query '{"match_all":{}}' \
    --from-date 2024-01-16 \
    --to-date 2024-01-18 \
    -o "$OUTFILE" || true

if [ -f "$OUTFILE" ]; then
    LINE_COUNT=$(wc -l < "$OUTFILE" | tr -d ' ')
    # Docs 3,4 (Jan 16), 5,6 (Jan 17) = 4 docs. Jan 18 excluded (to-date is exclusive).
    if [ "$LINE_COUNT" = "4" ]; then
        ok "date range export: got ${LINE_COUNT} lines (expected 4)"
    else
        fail "date range export: got ${LINE_COUNT} lines (expected 4)"
        cat "$OUTFILE" >&2
    fi
else
    fail "date range export: output file not created"
fi

# ── Export Test 3: zstd compression ──────────────────────────────────────────

OUTFILE="$EXPORT_DIR/compressed-export.jsonl.zst"
run_export "zstd compressed export" \
    --index "$INDEX_NAME" \
    --query '{"match_all":{}}' \
    --compression zstd \
    -o "$OUTFILE" || true

if [ -f "$OUTFILE" ]; then
    if command -v zstd &>/dev/null; then
        DECOMPRESSED=$(zstd -d -c "$OUTFILE")
        LINE_COUNT=$(echo "$DECOMPRESSED" | wc -l | tr -d ' ')
        if [ "$LINE_COUNT" = "10" ]; then
            ok "zstd export: decompressed to ${LINE_COUNT} lines (expected 10)"
        else
            fail "zstd export: decompressed to ${LINE_COUNT} lines (expected 10)"
        fi
    else
        # Check it's not plaintext JSON (zstd magic bytes: 0x28 0xB5 0x2F 0xFD)
        MAGIC=$(xxd -l 4 "$OUTFILE" | head -1)
        if echo "$MAGIC" | grep -q "28b5 2ffd"; then
            ok "zstd export: file has zstd magic bytes"
        else
            fail "zstd export: file does not appear to be zstd compressed"
        fi
    fi
else
    fail "zstd export: output file not created"
fi

# ── Export Test 4: _source field filtering via query-file ─────────────────────

QUERY_FILE="$EXPORT_DIR/filtered-query.json"
cat > "$QUERY_FILE" << 'QUERYEOF'
{
  "query": {
    "match_all": {}
  },
  "_source": ["@timestamp", "level", "host"]
}
QUERYEOF

OUTFILE="$EXPORT_DIR/filtered-export.jsonl"
run_export "_source field filtering" \
    --index "$INDEX_NAME" \
    --query-file "$QUERY_FILE" \
    -o "$OUTFILE" || true

if [ -f "$OUTFILE" ]; then
    LINE_COUNT=$(wc -l < "$OUTFILE" | tr -d ' ')
    if [ "$LINE_COUNT" = "10" ]; then
        ok "_source filtering: got ${LINE_COUNT} lines (expected 10)"
    else
        fail "_source filtering: got ${LINE_COUNT} lines (expected 10)"
    fi
    # Check that only requested fields are present
    FIRST_LINE=$(head -1 "$OUTFILE")
    HAS_MESSAGE=$(echo "$FIRST_LINE" | jq 'has("message")' 2>/dev/null || echo "error")
    HAS_TIMESTAMP=$(echo "$FIRST_LINE" | jq 'has("@timestamp")' 2>/dev/null || echo "error")
    HAS_LEVEL=$(echo "$FIRST_LINE" | jq 'has("level")' 2>/dev/null || echo "error")
    if [ "$HAS_MESSAGE" = "false" ] && [ "$HAS_TIMESTAMP" = "true" ] && [ "$HAS_LEVEL" = "true" ]; then
        ok "_source filtering: only requested fields present"
    else
        fail "_source filtering: unexpected fields (message=$HAS_MESSAGE, @timestamp=$HAS_TIMESTAMP, level=$HAS_LEVEL)"
        echo "    First line: $FIRST_LINE"
    fi
else
    fail "_source filtering: output file not created"
fi

# ── Export Test 5: alias resolution ──────────────────────────────────────────

OUTFILE="$EXPORT_DIR/alias-export.jsonl"
run_export "alias resolution" \
    --index "$ALIAS_NAME" \
    --query '{"match_all":{}}' \
    -o "$OUTFILE" || true

if [ -f "$OUTFILE" ]; then
    LINE_COUNT=$(wc -l < "$OUTFILE" | tr -d ' ')
    if [ "$LINE_COUNT" = "10" ]; then
        ok "alias export: got ${LINE_COUNT} lines via alias (expected 10)"
    else
        fail "alias export: got ${LINE_COUNT} lines via alias (expected 10)"
    fi
else
    fail "alias export: output file not created"
fi

# ── Export Test 6: combined date range + term filter ─────────────────────────

OUTFILE="$EXPORT_DIR/combined-export.jsonl"
run_export "combined date range + term filter" \
    --index "$INDEX_NAME" \
    --query '{"term":{"level":"error"}}' \
    --from-date 2024-01-15 \
    --to-date 2024-01-18 \
    -o "$OUTFILE" || true

if [ -f "$OUTFILE" ]; then
    LINE_COUNT=$(wc -l < "$OUTFILE" | tr -d ' ')
    # error docs in range: id 2 (Jan 15), id 5 (Jan 17) = 2 docs. Jan 18 excluded.
    if [ "$LINE_COUNT" = "2" ]; then
        ok "combined filter: got ${LINE_COUNT} lines (expected 2)"
    else
        fail "combined filter: got ${LINE_COUNT} lines (expected 2)"
        cat "$OUTFILE" >&2
    fi
else
    fail "combined filter: output file not created"
fi

# ── Export Test 7: full search body with sort and date range ──────────────────

QUERY_FILE="$EXPORT_DIR/full-body-query.json"
cat > "$QUERY_FILE" << 'QUERYEOF'
{
  "sort": [
    { "@timestamp": "asc" }
  ],
  "track_total_hits": true,
  "_source": ["@timestamp", "message", "level"],
  "query": {
    "bool": {
      "filter": [
        {
          "terms": {
            "level": ["info", "warn"]
          }
        }
      ]
    }
  }
}
QUERYEOF

OUTFILE="$EXPORT_DIR/full-body-export.jsonl"
run_export "full search body (sort + _source + query)" \
    --index "$INDEX_NAME" \
    --query-file "$QUERY_FILE" \
    --from-date 2024-01-16 \
    --to-date 2024-01-20 \
    -o "$OUTFILE" || true

if [ -f "$OUTFILE" ]; then
    LINE_COUNT=$(wc -l < "$OUTFILE" | tr -d ' ')
    # info+warn from Jan 16-19: id 3(info,Jan16), 4(warn,Jan16), 6(info,Jan17), 8(info,Jan18), 9(warn,Jan19), 10(info,Jan19) = 6
    if [ "$LINE_COUNT" = "6" ]; then
        ok "full body export: got ${LINE_COUNT} lines (expected 6)"
    else
        fail "full body export: got ${LINE_COUNT} lines (expected 6)"
        cat "$OUTFILE" >&2
    fi
    # Verify sort order: first timestamp should be earliest
    FIRST_TS=$(head -1 "$OUTFILE" | jq -r '.["@timestamp"]' 2>/dev/null || echo "error")
    LAST_TS=$(tail -1 "$OUTFILE" | jq -r '.["@timestamp"]' 2>/dev/null || echo "error")
    if [[ "$FIRST_TS" < "$LAST_TS" ]] || [[ "$FIRST_TS" = "$LAST_TS" ]]; then
        ok "full body export: results sorted by @timestamp ascending"
    else
        fail "full body export: results not sorted (first=$FIRST_TS, last=$LAST_TS)"
    fi
    # Verify _source filtering
    HAS_COUNT=$(head -1 "$OUTFILE" | jq 'has("count")' 2>/dev/null || echo "error")
    if [ "$HAS_COUNT" = "false" ]; then
        ok "full body export: _source filtering applied (no 'count' field)"
    else
        fail "full body export: _source filtering not applied (count field present)"
    fi
else
    fail "full body export: output file not created"
fi

# ══════════════════════════════════════════════════════════════════════════════
# List-snapshots and auto-discovery tests
# ══════════════════════════════════════════════════════════════════════════════

log ""
log "============================================"
log "  Running snapshots & auto-discovery tests"
log "============================================"
log ""

S3_COMMON_ARGS=(
    --bucket "$MINIO_BUCKET"
    --endpoint "$MINIO_ENDPOINT"
    --region us-east-1
    --access-key "$MINIO_ROOT_USER"
    --secret-key "$MINIO_ROOT_PASSWORD"
)

# ── List-snapshots Test 1: list all snapshots ─────────────────────────────────

echo -e "\033[1;34m==>\033[0m Test: snapshots (all)" >&2
LIST_OUTPUT=$("$JAVA" -jar "$JAR_PATH" snapshots "${S3_COMMON_ARGS[@]}" 2>/dev/null) || true

if echo "$LIST_OUTPUT" | grep -q "$SNAPSHOT_NAME"; then
    ok "snapshots: found snapshot [${SNAPSHOT_NAME}]"
else
    fail "snapshots: snapshot [${SNAPSHOT_NAME}] not in output"
    echo "    Output: $LIST_OUTPUT"
fi

if echo "$LIST_OUTPUT" | grep -q "SUCCESS"; then
    ok "snapshots: shows SUCCESS state"
else
    fail "snapshots: missing SUCCESS state"
fi

# ── List-snapshots Test 2: filter by index name ──────────────────────────────

echo -e "\033[1;34m==>\033[0m Test: snapshots --index (by name)" >&2
LIST_OUTPUT=$("$JAVA" -jar "$JAR_PATH" snapshots "${S3_COMMON_ARGS[@]}" --index "$INDEX_NAME" 2>/dev/null) || true

if echo "$LIST_OUTPUT" | grep -q "$SNAPSHOT_NAME"; then
    ok "snapshots --index: found snapshot for index [${INDEX_NAME}]"
else
    fail "snapshots --index: snapshot not found for index [${INDEX_NAME}]"
    echo "    Output: $LIST_OUTPUT"
fi

# ── List-snapshots Test 3: filter by alias ────────────────────────────────────

echo -e "\033[1;34m==>\033[0m Test: snapshots --index (by alias)" >&2
LIST_OUTPUT=$("$JAVA" -jar "$JAR_PATH" snapshots "${S3_COMMON_ARGS[@]}" --index "$ALIAS_NAME" 2>/dev/null) || true

if echo "$LIST_OUTPUT" | grep -q "$SNAPSHOT_NAME"; then
    ok "snapshots --index (alias): found snapshot for alias [${ALIAS_NAME}]"
else
    fail "snapshots --index (alias): snapshot not found for alias [${ALIAS_NAME}]"
    echo "    Output: $LIST_OUTPUT"
fi

# ── Export Test 8: auto-discover snapshot ─────────────────────────────────────

OUTFILE="$EXPORT_DIR/auto-discover-export.jsonl"
echo -e "\033[1;34m==>\033[0m Export test: auto-discover snapshot (no --snapshot)" >&2
"$JAVA" -jar "$JAR_PATH" export \
    "${S3_COMMON_ARGS[@]}" \
    --index "$INDEX_NAME" \
    --query '{"match_all":{}}' \
    -o "$OUTFILE" 2>/dev/null || true

if [ -f "$OUTFILE" ]; then
    LINE_COUNT=$(wc -l < "$OUTFILE" | tr -d ' ')
    if [ "$LINE_COUNT" = "10" ]; then
        ok "auto-discover export: got ${LINE_COUNT} lines without --snapshot (expected 10)"
    else
        fail "auto-discover export: got ${LINE_COUNT} lines (expected 10)"
    fi
else
    fail "auto-discover export: output file not created"
fi

# ── Export Test 9: auto-discover via alias ────────────────────────────────────

OUTFILE="$EXPORT_DIR/auto-discover-alias-export.jsonl"
echo -e "\033[1;34m==>\033[0m Export test: auto-discover snapshot via alias" >&2
"$JAVA" -jar "$JAR_PATH" export \
    "${S3_COMMON_ARGS[@]}" \
    --index "$ALIAS_NAME" \
    --query '{"match_all":{}}' \
    -o "$OUTFILE" 2>/dev/null || true

if [ -f "$OUTFILE" ]; then
    LINE_COUNT=$(wc -l < "$OUTFILE" | tr -d ' ')
    if [ "$LINE_COUNT" = "10" ]; then
        ok "auto-discover alias export: got ${LINE_COUNT} lines (expected 10)"
    else
        fail "auto-discover alias export: got ${LINE_COUNT} lines (expected 10)"
    fi
else
    fail "auto-discover alias export: output file not created"
fi

# ── List-snapshots Test 4: JSON output ───────────────────────────────────────

echo -e "\033[1;34m==>\033[0m Test: snapshots --json" >&2
LIST_JSON=$("$JAVA" -jar "$JAR_PATH" snapshots "${S3_COMMON_ARGS[@]}" --json 2>/dev/null) || true

if echo "$LIST_JSON" | jq -e '.[] | select(.snapshot == "'"$RANGE_SNAPSHOT_ONE"'") | .indices | index("'"$RANGE_INDEX_ONE"'")' >/dev/null 2>&1; then
    ok "snapshots --json: includes ${RANGE_SNAPSHOT_ONE} with ${RANGE_INDEX_ONE}"
else
    fail "snapshots --json: missing ${RANGE_SNAPSHOT_ONE} or indices payload"
    echo "    Output: $(echo "$LIST_JSON" | head -20)"
fi

# ── Export Test 10: export-range latest-per-index ────────────────────────────

RANGE_EXPORT_DIR="$EXPORT_DIR/export-range-latest"
RANGE_PROFILE="$EXPORT_DIR/export-range-latest-profile.json"
mkdir -p "$RANGE_EXPORT_DIR"
echo -e "\033[1;34m==>\033[0m Export test: export-range latest-per-index" >&2
"$JAVA" -jar "$JAR_PATH" export-range \
    "${S3_COMMON_ARGS[@]}" \
    --index-pattern '.ds-logs-180-default-*' \
    --index-date-from 2024-01-15 \
    --index-date-to 2024-01-16 \
    --query '{"match_all":{}}' \
    --from-date 2024-01-15 \
    --to-date 2024-01-17 \
    --output-dir "$RANGE_EXPORT_DIR" \
    --profile-file "$RANGE_PROFILE" 2>/dev/null || true

FILE_COUNT=$(find "$RANGE_EXPORT_DIR" -name '*.jsonl' | wc -l | tr -d ' ')
TOTAL_LINES=$(find "$RANGE_EXPORT_DIR" -name '*.jsonl' -exec cat {} \; | wc -l | tr -d ' ')
if [ "$FILE_COUNT" = "3" ]; then
    ok "export-range latest-per-index: created ${FILE_COUNT} files (expected 3 unique indices)"
else
    fail "export-range latest-per-index: created ${FILE_COUNT} files (expected 3)"
fi
if [ "$TOTAL_LINES" = "6" ]; then
    ok "export-range latest-per-index: exported ${TOTAL_LINES} docs (expected 6)"
else
    fail "export-range latest-per-index: exported ${TOTAL_LINES} docs (expected 6)"
fi

if find "$RANGE_EXPORT_DIR" -name "*${RANGE_INDEX_TWO}*.jsonl" | grep -q .; then
    ok "export-range latest-per-index: exported files named by index"
else
    fail "export-range latest-per-index: missing output for ${RANGE_INDEX_TWO}"
fi

if [ -f "$RANGE_PROFILE" ] &&
    echo "$(<"$RANGE_PROFILE")" | jq -e '.phases.query_parse_ms >= 0 and .s3.read_range_calls >= 0 and (.indices | length) == 3 and (.lucene_files | length) > 0' >/dev/null 2>&1; then
    ok "export-range latest-per-index: wrote profiling counters"
else
    fail "export-range latest-per-index: profiling output missing or invalid"
fi

# ── Export Test 11: export-range interrupted still writes profile ────────────

INTERRUPTED_PROFILE="$EXPORT_DIR/export-range-interrupted-profile.json"
INTERRUPTED_EXPORT_DIR="$EXPORT_DIR/export-range-interrupted"
mkdir -p "$INTERRUPTED_EXPORT_DIR"
echo -e "\033[1;34m==>\033[0m Export test: export-range interrupted writes profile" >&2
"$JAVA" -jar "$JAR_PATH" export-range \
    "${S3_COMMON_ARGS[@]}" \
    --index-pattern '.ds-logs-180-default-*' \
    --index-date-from 2024-01-15 \
    --index-date-to 2024-01-16 \
    --query '{"match_all":{}}' \
    --from-date 2024-01-15 \
    --to-date 2024-01-17 \
    --output-dir "$INTERRUPTED_EXPORT_DIR" \
    --profile-file "$INTERRUPTED_PROFILE" >/dev/null 2>&1 &
INTERRUPTED_PID=$!
for _ in $(seq 1 20); do
    if ! kill -0 "$INTERRUPTED_PID" >/dev/null 2>&1; then
        break
    fi
    kill -INT "$INTERRUPTED_PID" >/dev/null 2>&1 || true
    sleep 0.05
done
wait "$INTERRUPTED_PID" >/dev/null 2>&1 || true

if [ -f "$INTERRUPTED_PROFILE" ] &&
    echo "$(<"$INTERRUPTED_PROFILE")" | jq -e '.summary.total_ms >= 0 and (.summary.exit_reason == "interrupted" or .summary.exit_reason == "completed")' >/dev/null 2>&1; then
    ok "export-range interrupted: left a valid profiling file"
else
    fail "export-range interrupted: profiling output missing or invalid"
fi

# ── Export Test 12: export-range --all-snapshots ─────────────────────────────

RANGE_EXPORT_ALL_DIR="$EXPORT_DIR/export-range-all"
mkdir -p "$RANGE_EXPORT_ALL_DIR"
echo -e "\033[1;34m==>\033[0m Export test: export-range --all-snapshots" >&2
"$JAVA" -jar "$JAR_PATH" export-range \
    "${S3_COMMON_ARGS[@]}" \
    --index-pattern '.ds-logs-180-default-*' \
    --index-date-from 2024-01-15 \
    --index-date-to 2024-01-16 \
    --query '{"match_all":{}}' \
    --from-date 2024-01-15 \
    --to-date 2024-01-17 \
    --output-dir "$RANGE_EXPORT_ALL_DIR" \
    --all-snapshots 2>/dev/null || true

FILE_COUNT=$(find "$RANGE_EXPORT_ALL_DIR" -name '*.jsonl' | wc -l | tr -d ' ')
TOTAL_LINES=$(find "$RANGE_EXPORT_ALL_DIR" -name '*.jsonl' -exec cat {} \; | wc -l | tr -d ' ')
if [ "$FILE_COUNT" = "4" ]; then
    ok "export-range --all-snapshots: created ${FILE_COUNT} files (expected 4 snapshot/index pairs)"
else
    fail "export-range --all-snapshots: created ${FILE_COUNT} files (expected 4)"
fi
if [ "$TOTAL_LINES" = "8" ]; then
    ok "export-range --all-snapshots: exported ${TOTAL_LINES} docs (expected 8)"
else
    fail "export-range --all-snapshots: exported ${TOTAL_LINES} docs (expected 8)"
fi

# ── Results ──────────────────────────────────────────────────────────────────

log ""
log "============================================"
log "  Results: ${PASSED} passed, ${FAILED} failed"
log "============================================"

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
exit 0

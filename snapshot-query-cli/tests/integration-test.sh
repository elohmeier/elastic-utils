#!/usr/bin/env bash
#
# Integration test for elasticsearch-snapshot-query CLI (standalone JAR).
#
# Spins up MinIO (S3-compatible) and Elasticsearch via podman,
# indexes test documents, creates a snapshot to MinIO, stops ES,
# then queries the snapshot offline using the standalone JAR.
#
# Prerequisites:
#   - podman
#   - curl
#   - jq
#   - Java 21+
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

# ── Cleanup ──────────────────────────────────────────────────────────────────

cleanup() {
    log "Cleaning up..."
    if podman container exists "$ES_CONTAINER" 2>/dev/null; then
        log "Stopping Elasticsearch container"
        podman rm -f "$ES_CONTAINER" >/dev/null 2>&1 || true
    fi
    if podman container exists "$MINIO_CONTAINER" 2>/dev/null; then
        log "Stopping MinIO container"
        podman rm -f "$MINIO_CONTAINER" >/dev/null 2>&1 || true
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
    JAR_PATH="$PROJECT_ROOT/build/libs/elasticsearch-snapshot-query-cli-0.1.0.jar"
fi

if [ "$SKIP_BUILD" = false ]; then
    log "Building standalone JAR..."
    cd "$PROJECT_ROOT"
    JAVA_HOME="${JAVA_HOME:-}" ./gradlew shadowJar 2>&1 | tail -5
fi

[ -f "$JAR_PATH" ] || die "JAR not found at $JAR_PATH. Run without --skip-build."
log "Using JAR: $JAR_PATH"

# ── Step 2: Start MinIO ─────────────────────────────────────────────────────

log "Starting MinIO via podman..."
podman rm -f "$MINIO_CONTAINER" 2>/dev/null || true

podman run -d \
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
    podman exec "$MINIO_CONTAINER" \
        mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1
    podman exec "$MINIO_CONTAINER" \
        mc mb "local/${MINIO_BUCKET}" 2>/dev/null || true
fi

# ── Step 3: Start Elasticsearch (containerized) ─────────────────────────────

log "Starting Elasticsearch ${ES_VERSION} via podman..."
podman rm -f "$ES_CONTAINER" 2>/dev/null || true

podman run -d \
    --name "$ES_CONTAINER" \
    -p "${ES_HTTP_PORT}:9200" \
    -e "discovery.type=single-node" \
    -e "xpack.security.enabled=false" \
    -e "xpack.ml.enabled=false" \
    -e "ES_JAVA_OPTS=-Xms512m -Xmx512m" \
    -e "s3.client.default.endpoint=http://host.containers.internal:${MINIO_PORT}" \
    -e "s3.client.default.region=us-east-1" \
    -e "s3.client.default.path_style_access=true" \
    docker.io/elasticsearch/elasticsearch:${ES_VERSION}

log "Waiting for Elasticsearch to be ready..."
wait_for_url "http://localhost:${ES_HTTP_PORT}/_cluster/health" 120 || die "Elasticsearch failed to start"

# Install repository-s3 plugin and set credentials via keystore
log "Installing repository-s3 plugin..."
podman exec "$ES_CONTAINER" bash -c \
    'if ! bin/elasticsearch-plugin list | grep -q repository-s3; then
        bin/elasticsearch-plugin install --batch repository-s3
    fi'

# Set S3 credentials in keystore
podman exec "$ES_CONTAINER" bash -c \
    "echo '${MINIO_ROOT_USER}' | bin/elasticsearch-keystore add -xf s3.client.default.access_key && \
     echo '${MINIO_ROOT_PASSWORD}' | bin/elasticsearch-keystore add -xf s3.client.default.secret_key"

# Restart ES to pick up the plugin and keystore
log "Restarting Elasticsearch to load repository-s3 plugin..."
podman restart "$ES_CONTAINER"
wait_for_url "http://localhost:${ES_HTTP_PORT}/_cluster/health" 120 || die "Elasticsearch failed to restart"
log "Elasticsearch is ready"

# ── Step 4: Index test documents ─────────────────────────────────────────────

log "Creating index [${INDEX_NAME}] and indexing test documents..."

es_api PUT "/${INDEX_NAME}" -d '{
  "settings": {
    "number_of_shards": 2,
    "number_of_replicas": 0
  },
  "mappings": {
    "properties": {
      "message":   { "type": "text" },
      "level":     { "type": "keyword" },
      "status":    { "type": "keyword" },
      "timestamp": { "type": "date" },
      "count":     { "type": "long" },
      "host":      { "type": "keyword" }
    }
  }
}' | jq -r '.acknowledged' || die "Failed to create index"

es_api POST "/_bulk" -d '
{"index":{"_index":"test-logs","_id":"1"}}
{"message":"Server started successfully","level":"info","status":"ok","timestamp":"2024-01-15T10:00:00Z","count":1,"host":"web-01"}
{"index":{"_index":"test-logs","_id":"2"}}
{"message":"Connection timeout to database","level":"error","status":"error","timestamp":"2024-01-15T10:01:00Z","count":3,"host":"web-01"}
{"index":{"_index":"test-logs","_id":"3"}}
{"message":"Request processed in 250ms","level":"info","status":"ok","timestamp":"2024-01-15T10:02:00Z","count":42,"host":"web-02"}
{"index":{"_index":"test-logs","_id":"4"}}
{"message":"Disk usage above 90%","level":"warn","status":"warning","timestamp":"2024-01-15T10:03:00Z","count":1,"host":"web-03"}
{"index":{"_index":"test-logs","_id":"5"}}
{"message":"Authentication failed for user admin","level":"error","status":"error","timestamp":"2024-01-15T10:04:00Z","count":5,"host":"web-02"}
{"index":{"_index":"test-logs","_id":"6"}}
{"message":"Cache cleared","level":"info","status":"ok","timestamp":"2024-01-15T10:05:00Z","count":1,"host":"web-01"}
{"index":{"_index":"test-logs","_id":"7"}}
{"message":"Out of memory error","level":"error","status":"error","timestamp":"2024-01-15T10:06:00Z","count":1,"host":"web-03"}
{"index":{"_index":"test-logs","_id":"8"}}
{"message":"Backup completed","level":"info","status":"ok","timestamp":"2024-01-15T10:07:00Z","count":1,"host":"web-01"}
{"index":{"_index":"test-logs","_id":"9"}}
{"message":"SSL certificate expiring soon","level":"warn","status":"warning","timestamp":"2024-01-15T10:08:00Z","count":2,"host":"web-02"}
{"index":{"_index":"test-logs","_id":"10"}}
{"message":"New deployment rolled out","level":"info","status":"ok","timestamp":"2024-01-15T10:09:00Z","count":1,"host":"web-03"}
' | jq -r '.errors' || die "Failed to bulk index"

es_api POST "/${INDEX_NAME}/_flush" >/dev/null 2>&1 || true
es_api POST "/${INDEX_NAME}/_refresh" >/dev/null 2>&1 || true

DOC_COUNT=$(es_api GET "/${INDEX_NAME}/_count" | jq -r '.count')
log "Indexed ${DOC_COUNT} documents"

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

# ── Step 6: Stop Elasticsearch ───────────────────────────────────────────────

log "Stopping Elasticsearch..."
podman stop "$ES_CONTAINER" >/dev/null 2>&1 || true
log "Elasticsearch stopped — now querying snapshot offline"

# ── Step 7: Run CLI queries against the snapshot ─────────────────────────────

log ""
log "============================================"
log "  Running snapshot-query CLI tests"
log "============================================"
log ""

JAVA="${JAVA_HOME:+${JAVA_HOME}/bin/}java"

run_query() {
    local desc="$1" query="$2"
    shift 2
    echo -e "\033[1;34m==>\033[0m Test: ${desc}" >&2
    local raw_output json_output
    raw_output=$("$JAVA" -jar "$JAR_PATH" \
        --bucket "$MINIO_BUCKET" \
        --endpoint "$MINIO_ENDPOINT" \
        --region us-east-1 \
        --access-key "$MINIO_ROOT_USER" \
        --secret-key "$MINIO_ROOT_PASSWORD" \
        --snapshot "$SNAPSHOT_NAME" \
        --index "$INDEX_NAME" \
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

# ── Results ──────────────────────────────────────────────────────────────────

log ""
log "============================================"
log "  Results: ${PASSED} passed, ${FAILED} failed"
log "============================================"

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
exit 0

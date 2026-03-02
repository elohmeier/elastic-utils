#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Generate a runnable export script from `elastic-utils search plan-export`.

Usage:
  scripts/generate-export-script.sh --index my-alias --query-file query.json

Options:
  --index <name>            Index or alias (required)
  --query-file <path>       Query file for export calls (required)
  --output-script <path>    Output script path (default: tmp/run-exports.sh)
  --plan-json <path>        Optional path to persist planner JSON (default: tmp/export-plan.json)
  --prefix <name>           Output filename prefix (default: export)
  --window <month|week>     Planner window granularity (default: week)
  --target-docs <int>       Planner target docs per batch (default: 200000000)
  --compression <codec>     Export compression: zstd|none (default: zstd)
  --min-page-size <int>     Export min page size (default: 1000)
  --max-page-size <int>     Export max page size (default: 5000)
  --cold-workers <int>      Workers for dominant cold batches (default: 8)
  --frozen-workers <int>    Workers for dominant frozen batches (default: 2)
  --default-workers <int>   Workers for all other batches (default: 4)
EOF
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: required command not found: $1" >&2
        exit 1
    fi
}

INDEX=""
QUERY_FILE=""
OUTPUT_SCRIPT="tmp/run-exports.sh"
PLAN_JSON="tmp/export-plan.json"
PREFIX="export"
WINDOW="week"
TARGET_DOCS="200000000"
COMPRESSION="zstd"
MIN_PAGE_SIZE="1000"
MAX_PAGE_SIZE="5000"
COLD_WORKERS="8"
FROZEN_WORKERS="2"
DEFAULT_WORKERS="4"

while [[ $# -gt 0 ]]; do
    case "$1" in
    --index)
        INDEX="${2:-}"
        shift 2
        ;;
    --query-file)
        QUERY_FILE="${2:-}"
        shift 2
        ;;
    --output-script)
        OUTPUT_SCRIPT="${2:-}"
        shift 2
        ;;
    --plan-json)
        PLAN_JSON="${2:-}"
        shift 2
        ;;
    --prefix)
        PREFIX="${2:-}"
        shift 2
        ;;
    --window)
        WINDOW="${2:-}"
        shift 2
        ;;
    --target-docs)
        TARGET_DOCS="${2:-}"
        shift 2
        ;;
    --compression)
        COMPRESSION="${2:-}"
        shift 2
        ;;
    --min-page-size)
        MIN_PAGE_SIZE="${2:-}"
        shift 2
        ;;
    --max-page-size)
        MAX_PAGE_SIZE="${2:-}"
        shift 2
        ;;
    --cold-workers)
        COLD_WORKERS="${2:-}"
        shift 2
        ;;
    --frozen-workers)
        FROZEN_WORKERS="${2:-}"
        shift 2
        ;;
    --default-workers)
        DEFAULT_WORKERS="${2:-}"
        shift 2
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        echo "error: unknown option: $1" >&2
        usage >&2
        exit 1
        ;;
    esac
done

if [[ -z "$INDEX" || -z "$QUERY_FILE" ]]; then
    echo "error: --index and --query-file are required" >&2
    usage >&2
    exit 1
fi

if [[ "$WINDOW" != "month" && "$WINDOW" != "week" ]]; then
    echo "error: --window must be month or week" >&2
    exit 1
fi

if [[ "$COMPRESSION" != "zstd" && "$COMPRESSION" != "none" ]]; then
    echo "error: --compression must be zstd or none" >&2
    exit 1
fi

require_cmd elastic-utils
require_cmd jq

mkdir -p "$(dirname "$OUTPUT_SCRIPT")"
mkdir -p "$(dirname "$PLAN_JSON")"

elastic-utils search plan-export \
    --index "$INDEX" \
    --window "$WINDOW" \
    --target-docs "$TARGET_DOCS" \
    --output json >"$PLAN_JSON"

jq -r \
    --arg index "$INDEX" \
    --arg query_file "$QUERY_FILE" \
    --arg prefix "$PREFIX" \
    --arg compression "$COMPRESSION" \
    --arg min_page_size "$MIN_PAGE_SIZE" \
    --arg max_page_size "$MAX_PAGE_SIZE" \
    --argjson cold_workers "$COLD_WORKERS" \
    --argjson frozen_workers "$FROZEN_WORKERS" \
    --argjson default_workers "$DEFAULT_WORKERS" \
    '
  def workers_for_tier($tier):
    if $tier == "cold" then $cold_workers
    elif $tier == "frozen" then $frozen_workers
    else $default_workers
    end;

  def output_name($from; $to):
    if $compression == "zstd" then
      "\($prefix)-\($from)_to_\($to).jsonl.zst"
    else
      "\($prefix)-\($from)_to_\($to).jsonl"
    end;

  def export_cmd($b):
    "elastic-utils search export --index \"" + $index + "\" --query-file \"" + $query_file + "\" --from-date " +
    $b.from_date + " --to-date " + $b.to_date + " -o \"" + $b.outfile + "\" " +
    "--compression " + $compression + " --min-page-size " + $min_page_size +
    " --max-page-size " + $max_page_size + " --workers " + ($b.workers|tostring);

  [
    "#!/usr/bin/env bash",
    "set -euxo pipefail",
    "",
    "# Generated from: elastic-utils search plan-export",
    "# index: " + $index,
    "# query-file: " + $query_file,
    "",
    (
      reduce (
        .suggested_batches[]
        | .tier = (.dominant_tier // "unknown")
        | .workers = workers_for_tier(.tier)
        | .outfile = output_name(.from_date; .to_date)
      ) as $b (
        {lines: [], last_tier: ""};
        .lines += (
          (if .last_tier != $b.tier then
            [
              "",
              "# --- " + ($b.tier | ascii_upcase) + " section starts here ---"
            ]
          else
            []
          end)
          + [
            "# batch: " + $b.from_date + " -> " + $b.to_date
              + " | tier=" + $b.tier
              + " | docs~" + (($b.docs // 0) | tostring)
              + " | workers=" + (($b.workers // 0) | tostring),
            export_cmd($b)
          ]
        )
        | .last_tier = $b.tier
      )
      | .lines[]
    )
  ] | .[]
  ' "$PLAN_JSON" >"$OUTPUT_SCRIPT"

chmod +x "$OUTPUT_SCRIPT"
echo "Wrote export plan JSON to $PLAN_JSON"
echo "Wrote runnable export script to $OUTPUT_SCRIPT"

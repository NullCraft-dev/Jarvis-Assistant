#!/usr/bin/env bash

set -u

GATEWAY_URL="${JARVIS_GATEWAY_URL:-http://127.0.0.1:8080/api}"
TIMEOUT_SECONDS="${JARVIS_CHECK_TIMEOUT_SECONDS:-15}"
REQUIRE_WORKER="${JARVIS_CHECK_REQUIRE_WORKER:-1}"
REQUIRE_COMPLETION="${JARVIS_CHECK_REQUIRE_COMPLETION:-1}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing command: $1" >&2
    exit 1
  fi
}

json_get_task_run() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
if not d.get("ok"):
    raise SystemExit("create task returned ok=false")
data = d.get("data") or {}
task = data.get("task") or {}
run = data.get("run") or {}
task_id = task.get("id", "")
run_id = run.get("id", "")
if not task_id or not run_id:
    raise SystemExit("create task response missing task.id or run.id")
print(task_id, run_id)
'
}

check_workers() {
  local workers_file="$1"
  python3 - "$REQUIRE_WORKER" "$workers_file" <<'PY'
import json, sys

require_worker = sys.argv[1] == "1"
with open(sys.argv[2], "r", encoding="utf-8") as f:
    d = json.load(f)
if not d.get("ok"):
    raise SystemExit("workers endpoint returned ok=false")
workers = (d.get("data") or {}).get("workers") or []
active = [w for w in workers if not w.get("is_stale")]
summary = ", ".join(
    f"{w.get('worker_id')}:{w.get('status')}" for w in workers
) or "none"
print(f"workers={len(workers)} active={len(active)} [{summary}]")
if require_worker and not active:
    raise SystemExit("no active worker heartbeat found")
PY
}

check_events() {
  local events_file="$1"
  python3 - "$REQUIRE_COMPLETION" "$events_file" <<'PY'
import json, sys

require_completion = sys.argv[1] == "1"
events = []
with open(sys.argv[2], "r", encoding="utf-8") as f:
    lines = list(f)
for raw in lines:
    line = raw.strip()
    if not line.startswith("data:"):
        continue
    payload = line.split("data:", 1)[1].strip()
    if not payload:
        continue
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        continue
    events.append(event)

types = [e.get("type", "") for e in events]
print("events=" + str(len(events)) + " [" + ", ".join(types) + "]")

if "task.created" not in types:
    raise SystemExit("missing task.created event")
if require_completion and "agent.run.completed" not in types:
    raise SystemExit("missing agent.run.completed event")
PY
}

need_cmd curl
need_cmd python3

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "gateway=$GATEWAY_URL"

echo "checking health..."
if ! curl -fsS "$GATEWAY_URL/health" > "$TMP_DIR/health.json"; then
  echo "gateway health check failed" >&2
  exit 1
fi

echo "checking worker heartbeat view..."
if ! curl -fsS "$GATEWAY_URL/runtime/workers" > "$TMP_DIR/workers.json"; then
  echo "worker status endpoint failed" >&2
  exit 1
fi
if ! check_workers "$TMP_DIR/workers.json"; then
  exit 1
fi

echo "creating task..."
if ! curl -fsS \
  -H "Content-Type: application/json" \
  -d '{"user_goal":"dev runtime smoke check"}' \
  "$GATEWAY_URL/tasks" > "$TMP_DIR/create-task.json"; then
  echo "create task failed" >&2
  exit 1
fi

if ! read -r TASK_ID RUN_ID < <(json_get_task_run < "$TMP_DIR/create-task.json"); then
  exit 1
fi
echo "task_id=$TASK_ID"
echo "run_id=$RUN_ID"

echo "reading SSE events for ${TIMEOUT_SECONDS}s..."
curl -fsS -N --max-time "$TIMEOUT_SECONDS" \
  "$GATEWAY_URL/runs/$RUN_ID/events" > "$TMP_DIR/events.sse" 2> "$TMP_DIR/curl-events.err"
curl_code=$?
if [ "$curl_code" -ne 0 ] && [ "$curl_code" -ne 28 ]; then
  cat "$TMP_DIR/curl-events.err" >&2
  echo "SSE read failed with curl exit code $curl_code" >&2
  exit 1
fi

if ! check_events "$TMP_DIR/events.sse"; then
  echo "raw SSE output:" >&2
  cat "$TMP_DIR/events.sse" >&2
  exit 1
fi

echo "runtime smoke check passed"

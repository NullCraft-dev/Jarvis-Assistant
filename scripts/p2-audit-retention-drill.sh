#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="$REPO_DIR/apps/agent-worker"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SAFE_STAMP="$(printf '%s' "$RUN_STAMP" | tr '[:upper:]' '[:lower:]')"
CONTAINER_NAME="jarvis-p2-audit-retention-$SAFE_STAMP-$RANDOM"
VOLUME_NAME="jarvis-p2-audit-retention-data-$SAFE_STAMP-$RANDOM"
DATABASE_NAME="jarvis_p2_audit_retention"
DATABASE_USER="jarvis_p2"
DATABASE_PASSWORD="jarvis-p2-isolated"
POSTGRES_IMAGE="${JARVIS_P2_POSTGRES_IMAGE:-pgvector/pgvector:0.8.2-pg16-bookworm}"
OUTPUT_BASE="${JARVIS_P2_OUTPUT_DIR:-$REPO_DIR/.local/p2-hardening/audit-retention-drill}"
OUTPUT_DIR="$OUTPUT_BASE/$RUN_STAMP"
SUMMARY_FILE="$OUTPUT_DIR/summary.json"
STATUS_FILE="$OUTPUT_DIR/container-cleanup.txt"
CONTAINER_CREATED=0
VOLUME_CREATED=0
DRILL_FINISHED=0

fail() {
  printf '[p2-audit-retention-drill] FAILED: %s\n' "$*" >&2
  exit 1
}

validate_targets() {
  [[ "$CONTAINER_NAME" =~ ^jarvis-p2-audit-retention-[a-z0-9-]+$ ]] \
    || fail "临时容器名未通过安全校验"
  [[ "$VOLUME_NAME" =~ ^jarvis-p2-audit-retention-data-[a-z0-9-]+$ ]] \
    || fail "临时数据卷名未通过安全校验"
  [[ "$DATABASE_NAME" == jarvis_p2_audit_retention ]] \
    || fail "临时数据库名未通过安全校验"
}

validate_container_label() {
  local label
  label="$(docker inspect --format '{{ index .Config.Labels "com.jarvis.p2-drill" }}' "$CONTAINER_NAME" 2>/dev/null || true)"
  [ "$label" = "audit-retention" ] || fail "临时容器标签不匹配，拒绝删除"
}

validate_volume_label() {
  local label
  label="$(docker volume inspect --format '{{ index .Labels "com.jarvis.p2-drill" }}' "$VOLUME_NAME" 2>/dev/null || true)"
  [ "$label" = "audit-retention" ] || fail "临时数据卷标签不匹配，拒绝删除"
}

cleanup() {
  local exit_code="$?"
  set +e
  if [ "$CONTAINER_CREATED" -eq 1 ]; then
    validate_container_label
    docker rm -f "$CONTAINER_NAME" >/dev/null
    CONTAINER_CREATED=0
  fi
  if [ "$VOLUME_CREATED" -eq 1 ]; then
    validate_volume_label
    docker volume rm "$VOLUME_NAME" >/dev/null
    VOLUME_CREATED=0
  fi
  if [ "$DRILL_FINISHED" -eq 0 ]; then
    printf 'status=failed\nexit_code=%s\ncontainer_removed=%s\nvolume_removed=%s\n' \
      "$exit_code" "$((1 - CONTAINER_CREATED))" "$((1 - VOLUME_CREATED))" >"$STATUS_FILE"
  fi
  exit "$exit_code"
}

wait_postgres() {
  local attempt
  for attempt in $(seq 1 60); do
    if docker exec "$CONTAINER_NAME" pg_isready \
      -U "$DATABASE_USER" -d "$DATABASE_NAME" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "临时 PostgreSQL 在 60 秒内未就绪"
}

validate_targets
command -v docker >/dev/null 2>&1 || fail "缺少 docker"
command -v uv >/dev/null 2>&1 || fail "缺少 uv"
mkdir -p "$OUTPUT_DIR"
trap cleanup EXIT

docker volume create \
  --label com.jarvis.p2-drill=audit-retention \
  "$VOLUME_NAME" >/dev/null
VOLUME_CREATED=1

docker run -d \
  --name "$CONTAINER_NAME" \
  --label com.jarvis.p2-drill=audit-retention \
  -e "POSTGRES_USER=$DATABASE_USER" \
  -e "POSTGRES_PASSWORD=$DATABASE_PASSWORD" \
  -e "POSTGRES_DB=$DATABASE_NAME" \
  -p 127.0.0.1::5432 \
  -v "$VOLUME_NAME:/var/lib/postgresql/data" \
  "$POSTGRES_IMAGE" >/dev/null
CONTAINER_CREATED=1
wait_postgres

HOST_PORT="$(docker port "$CONTAINER_NAME" 5432/tcp | awk -F: 'NR == 1 {print $NF}')"
[[ "$HOST_PORT" =~ ^[0-9]+$ ]] || fail "无法解析临时 PostgreSQL 随机端口"
DATABASE_URL="postgresql+asyncpg://$DATABASE_USER:$DATABASE_PASSWORD@127.0.0.1:$HOST_PORT/$DATABASE_NAME"

(
  cd "$AGENT_DIR"
  JARVIS_DATABASE_URL="$DATABASE_URL" uv run alembic upgrade head
) >"$OUTPUT_DIR/migration.log" 2>&1

(
  cd "$AGENT_DIR"
  uv run python "$SCRIPT_DIR/p2-audit-retention-drill.py" \
    --database-url "$DATABASE_URL" \
    --expected-database "$DATABASE_NAME" \
    --output "$SUMMARY_FILE"
) >"$OUTPUT_DIR/drill.log" 2>&1

grep -Fq '"status": "passed"' "$SUMMARY_FILE" \
  || fail "隔离数据库演练未通过"

validate_container_label
docker rm -f "$CONTAINER_NAME" >/dev/null
CONTAINER_CREATED=0
validate_volume_label
docker volume rm "$VOLUME_NAME" >/dev/null
VOLUME_CREATED=0

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  fail "临时容器删除后仍存在"
fi
if docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
  fail "临时数据卷删除后仍存在"
fi

printf 'status=passed\ncontainer_removed=1\nvolume_removed=1\n' >"$STATUS_FILE"
DRILL_FINISHED=1
printf '[p2-audit-retention-drill] PASSED: %s\n' "$OUTPUT_DIR"

#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="$REPO_DIR/apps/agent-worker"
CONDA_ENV="${JARVIS_CONDA_ENV:-jarvis-assistant}"
DB_USER="${JARVIS_P2_DB_USER:-jarvis}"
SOURCE_DB="${JARVIS_P2_SOURCE_DB:-jarvis}"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SAFE_STAMP="$(printf '%s' "$RUN_STAMP" | tr '[:upper:]' '[:lower:]')"
RESTORE_DB="jarvis_p2_restore_${SAFE_STAMP}_$RANDOM"
OUTPUT_BASE="${JARVIS_P2_OUTPUT_DIR:-$REPO_DIR/.local/p2-hardening/data-recovery-drill}"
OUTPUT_DIR="$OUTPUT_BASE/$RUN_STAMP"
BACKUP_FILE="$OUTPUT_DIR/jarvis.dump"
SUMMARY_FILE="$OUTPUT_DIR/summary.txt"
COUNTS_FILE="$OUTPUT_DIR/critical-table-counts.tsv"
SOURCE_TABLES_FILE="$OUTPUT_DIR/source-tables.txt"
RESTORE_TABLES_FILE="$OUTPUT_DIR/restored-tables.txt"
RESTORE_CREATED=0
POSTGRES_WAS_RUNNING=0
DRILL_FINISHED=0

CRITICAL_TABLES=(
  tasks
  agent_runs
  execution_steps
  runtime_events
  tool_calls
  permission_requests
  audit_logs
  artifacts
  rag_documents
  rag_ingestion_jobs
  rag_chunks
  rag_chunk_embeddings
  outbox_events
  inbox_events
)

fail() {
  printf '[p2-data-drill] FAILED: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"
}

validate_identifiers() {
  [[ "$DB_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || fail "非法数据库用户标识"
  [[ "$SOURCE_DB" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || fail "非法源数据库标识"
  [[ "$RESTORE_DB" =~ ^jarvis_p2_restore_[a-z0-9_]+$ ]] || fail "临时恢复数据库名未通过安全校验"
  [ "$RESTORE_DB" != "$SOURCE_DB" ] || fail "临时恢复数据库不得等于源数据库"
}

compose_exec() {
  docker compose exec -T postgres "$@"
}

query_scalar() {
  local database="$1"
  local sql="$2"
  compose_exec psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$database" -Atqc "$sql" | tr -d '\r'
}

cleanup() {
  local exit_code="$?"
  set +e
  if [ "$RESTORE_CREATED" -eq 1 ]; then
    compose_exec dropdb -U "$DB_USER" --if-exists "$RESTORE_DB" >/dev/null 2>&1
  fi
  if [ "$POSTGRES_WAS_RUNNING" -eq 0 ]; then
    docker compose stop postgres >/dev/null 2>&1
  fi
  if [ "$DRILL_FINISHED" -eq 0 ] && [ -d "$OUTPUT_DIR" ]; then
    printf 'status=failed\nexit_code=%s\n' "$exit_code" >>"$SUMMARY_FILE"
  fi
  exit "$exit_code"
}

wait_postgres() {
  local attempt
  for attempt in $(seq 1 60); do
    if compose_exec pg_isready -U "$DB_USER" -d "$SOURCE_DB" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "PostgreSQL 在 60 秒内未就绪"
}

alembic_head() {
  local output heads
  output="$(
    cd "$AGENT_DIR"
    conda run --no-capture-output -n "$CONDA_ENV" python -m alembic heads
  )"
  heads="$(printf '%s\n' "$output" | awk '/\(head\)$/ {print $1}')"
  [ "$(printf '%s\n' "$heads" | sed '/^$/d' | wc -l | tr -d ' ')" = "1" ] \
    || fail "Alembic 必须且只能有一个 head"
  printf '%s' "$heads"
}

list_tables() {
  local database="$1"
  query_scalar "$database" \
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;"
}

compare_critical_counts() {
  local table source_count restored_count
  printf 'table\tsource_count\trestored_count\n' >"$COUNTS_FILE"
  for table in "${CRITICAL_TABLES[@]}"; do
    if ! grep -Fxq "$table" "$SOURCE_TABLES_FILE"; then
      fail "源数据库缺少关键表: $table"
    fi
    source_count="$(query_scalar "$SOURCE_DB" "SELECT count(*) FROM public.\"$table\";")"
    restored_count="$(query_scalar "$RESTORE_DB" "SELECT count(*) FROM public.\"$table\";")"
    printf '%s\t%s\t%s\n' "$table" "$source_count" "$restored_count" >>"$COUNTS_FILE"
    [ "$source_count" = "$restored_count" ] \
      || fail "关键表行数不一致: $table source=$source_count restored=$restored_count"
  done
}

need_cmd docker
need_cmd conda
need_cmd git
need_cmd shasum
validate_identifiers
mkdir -p "$OUTPUT_DIR"
trap cleanup EXIT

if [ -n "$(docker compose ps --status running -q postgres 2>/dev/null)" ]; then
  POSTGRES_WAS_RUNNING=1
fi

printf 'mode=p2-data-recovery-drill\nrevision=%s\nstarted_at=%s\nrestore_database=%s\n' \
  "$(git -C "$REPO_DIR" rev-parse HEAD)" "$RUN_STAMP" "$RESTORE_DB" >"$SUMMARY_FILE"

docker compose up -d postgres >/dev/null
wait_postgres

CODE_HEAD="$(alembic_head)"
SOURCE_HEAD="$(query_scalar "$SOURCE_DB" 'SELECT version_num FROM alembic_version;')"
[ "$CODE_HEAD" = "$SOURCE_HEAD" ] \
  || fail "源数据库 migration 落后或分叉: code=$CODE_HEAD database=$SOURCE_HEAD"

compose_exec pg_dump -U "$DB_USER" -d "$SOURCE_DB" -Fc --no-owner --no-privileges >"$BACKUP_FILE"
[ -s "$BACKUP_FILE" ] || fail "备份文件为空"
compose_exec pg_restore --list <"$BACKUP_FILE" >"$OUTPUT_DIR/backup-contents.txt"
[ -s "$OUTPUT_DIR/backup-contents.txt" ] || fail "pg_restore 无法读取备份目录"

compose_exec createdb -U "$DB_USER" "$RESTORE_DB"
RESTORE_CREATED=1
compose_exec pg_restore -U "$DB_USER" -d "$RESTORE_DB" \
  --no-owner --no-privileges --exit-on-error <"$BACKUP_FILE" >/dev/null

RESTORED_HEAD="$(query_scalar "$RESTORE_DB" 'SELECT version_num FROM alembic_version;')"
[ "$SOURCE_HEAD" = "$RESTORED_HEAD" ] \
  || fail "恢复库 migration revision 不一致: source=$SOURCE_HEAD restored=$RESTORED_HEAD"

list_tables "$SOURCE_DB" >"$SOURCE_TABLES_FILE"
list_tables "$RESTORE_DB" >"$RESTORE_TABLES_FILE"
diff -u "$SOURCE_TABLES_FILE" "$RESTORE_TABLES_FILE" >"$OUTPUT_DIR/table-diff.txt" \
  || fail "恢复库公共表集合与源库不一致"
compare_critical_counts

BACKUP_SHA256="$(shasum -a 256 "$BACKUP_FILE" | awk '{print $1}')"
BACKUP_BYTES="$(wc -c <"$BACKUP_FILE" | tr -d ' ')"
TABLE_COUNT="$(wc -l <"$SOURCE_TABLES_FILE" | tr -d ' ')"
printf 'code_head=%s\ndatabase_head=%s\nbackup_sha256=%s\nbackup_bytes=%s\npublic_table_count=%s\ncritical_table_count=%s\nstatus=passed\nfinished_at=%s\n' \
  "$CODE_HEAD" "$SOURCE_HEAD" "$BACKUP_SHA256" "$BACKUP_BYTES" "$TABLE_COUNT" \
  "${#CRITICAL_TABLES[@]}" "$(date -u +%Y%m%dT%H%M%SZ)" >>"$SUMMARY_FILE"

DRILL_FINISHED=1
printf '[p2-data-drill] PASSED: %s\n' "$OUTPUT_DIR"

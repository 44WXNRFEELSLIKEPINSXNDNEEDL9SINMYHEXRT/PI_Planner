#!/usr/bin/env bash
# PostgreSQL dump with restore verification. Runs inside the backup service.
set -euo pipefail

: "${PGDATABASE:?PGDATABASE is required}"
: "${BACKUP_RETENTION_DAYS:?BACKUP_RETENTION_DAYS is required}"

umask 077
mkdir -p /backups

started_epoch="$(date +%s)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="/backups/${PGDATABASE}-${stamp}.dump"
temporary="${archive}.partial"
verify_db="${PGDATABASE}_restore_check_${stamp}"
metrics_file="/backups/backup.prom"
last_success_file="/backups/.backup_last_success"
failures_file="/backups/.backup_failures"
archive_size=0
restore_enabled=0
restore_verified=0
success=0

cleanup() {
  dropdb --if-exists "$verify_db" >/dev/null 2>&1 || true
  rm -f "$temporary"
}

write_metrics() {
  local rc="$1"
  local finished_epoch duration failures last_success metrics_tmp
  finished_epoch="$(date +%s)"
  duration="$((finished_epoch - started_epoch))"
  failures="$(cat "$failures_file" 2>/dev/null || printf '0')"
  if [[ "$success" -eq 1 && "$rc" -eq 0 ]]; then
    printf '%s\n' "$finished_epoch" > "$last_success_file"
  else
    failures="$((failures + 1))"
    printf '%s\n' "$failures" > "$failures_file"
  fi
  last_success="$(cat "$last_success_file" 2>/dev/null || printf '0')"
  metrics_tmp="${metrics_file}.tmp"
  {
    printf '# HELP pi_planner_backup_last_attempt_timestamp_seconds Unix-время последней попытки backup\n'
    printf '# TYPE pi_planner_backup_last_attempt_timestamp_seconds gauge\n'
    printf 'pi_planner_backup_last_attempt_timestamp_seconds %s\n' "$finished_epoch"
    printf '# HELP pi_planner_backup_last_success_timestamp_seconds Unix-время последнего успешного backup\n'
    printf '# TYPE pi_planner_backup_last_success_timestamp_seconds gauge\n'
    printf 'pi_planner_backup_last_success_timestamp_seconds %s\n' "$last_success"
    printf '# HELP pi_planner_backup_duration_seconds Длительность последней попытки backup\n'
    printf '# TYPE pi_planner_backup_duration_seconds gauge\n'
    printf 'pi_planner_backup_duration_seconds %s\n' "$duration"
    printf '# HELP pi_planner_backup_size_bytes Размер последнего созданного архива\n'
    printf '# TYPE pi_planner_backup_size_bytes gauge\n'
    printf 'pi_planner_backup_size_bytes %s\n' "$archive_size"
    printf '# HELP pi_planner_backup_restore_verification_enabled Включена restore-проверка\n'
    printf '# TYPE pi_planner_backup_restore_verification_enabled gauge\n'
    printf 'pi_planner_backup_restore_verification_enabled %s\n' "$restore_enabled"
    printf '# HELP pi_planner_backup_restore_verified Успешна restore-проверка последнего backup\n'
    printf '# TYPE pi_planner_backup_restore_verified gauge\n'
    printf 'pi_planner_backup_restore_verified %s\n' "$restore_verified"
    printf '# HELP pi_planner_backup_failures_total Неуспешные попытки backup\n'
    printf '# TYPE pi_planner_backup_failures_total counter\n'
    printf 'pi_planner_backup_failures_total %s\n' "$failures"
  } > "$metrics_tmp"
  mv "$metrics_tmp" "$metrics_file"
}

finish() {
  local rc="$?"
  cleanup
  write_metrics "$rc"
}
trap finish EXIT

echo "[backup] creating ${archive##*/}"
pg_dump --format=custom --no-owner --no-privileges --file="$temporary" "$PGDATABASE"
mv "$temporary" "$archive"
archive_size="$(stat --format='%s' "$archive")"

if [[ "${BACKUP_VERIFY_RESTORE,,}" == "true" ]]; then
  restore_enabled=1
  echo "[backup] verifying restore in temporary database"
  createdb "$verify_db"
  pg_restore --exit-on-error --no-owner --no-privileges --dbname="$verify_db" "$archive"
  psql --dbname="$verify_db" --no-align --tuples-only --quiet \
    --command="SELECT to_regclass('public.load_batches') IS NOT NULL AND to_regclass('public.v_plan_violations') IS NOT NULL" \
    | grep -qx t
  dropdb "$verify_db"
  restore_verified=1
fi

find /backups -maxdepth 1 -type f -name "${PGDATABASE}-*.dump" \
  -mtime "+${BACKUP_RETENTION_DAYS}" -delete
success=1
echo "[backup] completed ${archive##*/}"

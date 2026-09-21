#!/usr/bin/env bash
# PostgreSQL dump with restore verification. Runs inside the backup service.
set -euo pipefail

: "${PGDATABASE:?PGDATABASE is required}"
: "${BACKUP_RETENTION_DAYS:?BACKUP_RETENTION_DAYS is required}"

umask 077
mkdir -p /backups

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="/backups/${PGDATABASE}-${stamp}.dump"
temporary="${archive}.partial"
verify_db="${PGDATABASE}_restore_check_${stamp}"

cleanup() {
  dropdb --if-exists "$verify_db" >/dev/null 2>&1 || true
  rm -f "$temporary"
}
trap cleanup EXIT

echo "[backup] creating ${archive##*/}"
pg_dump --format=custom --no-owner --no-privileges --file="$temporary" "$PGDATABASE"
mv "$temporary" "$archive"

if [[ "${BACKUP_VERIFY_RESTORE,,}" == "true" ]]; then
  echo "[backup] verifying restore in temporary database"
  createdb "$verify_db"
  pg_restore --exit-on-error --no-owner --no-privileges --dbname="$verify_db" "$archive"
  psql --dbname="$verify_db" --no-align --tuples-only --quiet \
    --command="SELECT to_regclass('public.load_batches') IS NOT NULL AND to_regclass('public.v_plan_violations') IS NOT NULL" \
    | grep -qx t
  dropdb "$verify_db"
fi

find /backups -maxdepth 1 -type f -name "${PGDATABASE}-*.dump" \
  -mtime "+${BACKUP_RETENTION_DAYS}" -delete
echo "[backup] completed ${archive##*/}"

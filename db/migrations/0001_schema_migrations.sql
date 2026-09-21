-- Baseline for databases created before versioned migrations were introduced.
-- New migration files contain only the schema delta and must be safe to run in
-- one transaction. Do not edit an applied migration: its SHA-256 is checked.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE schema_migrations IS
    'Applied database migrations. Managed only by tools/migrate.py.';

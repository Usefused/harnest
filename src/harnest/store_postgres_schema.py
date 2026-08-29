"""PostgreSQL schema owned by the built-in Harnest store."""

SCHEMA_VERSION = 4
SCHEMA_LOCK = 489_867_841_435_466_307

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS harnest_schema_migrations (
    component text PRIMARY KEY,
    version integer NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS harnest_sessions (
    user_id text NOT NULL,
    session_id text NOT NULL,
    state jsonb NOT NULL,
    application_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session_id)
);
ALTER TABLE harnest_sessions
ADD COLUMN IF NOT EXISTS application_data jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS harnest_runs (
    run_id text PRIMARY KEY,
    application_id text NOT NULL,
    user_id text NOT NULL,
    session_id text NOT NULL,
    framework text NOT NULL CHECK (framework IN ('adk', 'langgraph')),
    status text NOT NULL CHECK (
        status IN ('running', 'waiting', 'completed', 'failed', 'cancelled')
    ),
    revision integer NOT NULL DEFAULT 0,
    pending_action jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS harnest_one_active_run
ON harnest_runs (application_id, user_id, session_id)
WHERE status IN ('running', 'waiting');
CREATE TABLE IF NOT EXISTS harnest_checkpoints (
    run_id text NOT NULL REFERENCES harnest_runs(run_id) ON DELETE CASCADE,
    namespace text NOT NULL,
    checkpoint_id text NOT NULL,
    framework text NOT NULL CHECK (framework IN ('adk', 'langgraph')),
    type_name text NOT NULL,
    payload bytea NOT NULL,
    metadata_type text NOT NULL,
    metadata bytea NOT NULL,
    versions_type text NOT NULL,
    versions bytea NOT NULL,
    parent_checkpoint_id text,
    revision integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, namespace, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS harnest_checkpoint_history
ON harnest_checkpoints (run_id, namespace, revision DESC, checkpoint_id DESC);
CREATE TABLE IF NOT EXISTS harnest_checkpoint_writes (
    run_id text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    channel text NOT NULL,
    type_name text NOT NULL,
    payload bytea NOT NULL,
    task_path text NOT NULL,
    PRIMARY KEY (run_id, checkpoint_id, task_id, channel),
    FOREIGN KEY (run_id) REFERENCES harnest_runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS harnest_continuations (
    continuation_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES harnest_runs(run_id) ON DELETE CASCADE,
    application_id text NOT NULL,
    user_id text NOT NULL,
    session_id text NOT NULL,
    provider text NOT NULL,
    capability text NOT NULL,
    schema_id text NOT NULL,
    resume jsonb,
    external_id text NOT NULL,
    external_key text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('pending', 'completed', 'failed', 'claimed')
    ),
    revision integer NOT NULL DEFAULT 0,
    ready boolean NOT NULL DEFAULT false,
    result jsonb,
    failure jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (application_id, provider, external_key)
);
ALTER TABLE harnest_continuations
ADD COLUMN IF NOT EXISTS resume jsonb;
ALTER TABLE harnest_continuations
ADD COLUMN IF NOT EXISTS ready boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS harnest_pending_continuations
ON harnest_continuations (application_id, provider, continuation_id)
WHERE status='pending';
INSERT INTO harnest_schema_migrations(component, version)
VALUES ('store', 4)
ON CONFLICT (component) DO UPDATE
SET version=EXCLUDED.version, applied_at=now()
WHERE harnest_schema_migrations.version < EXCLUDED.version;
"""

__all__ = ["SCHEMA_LOCK", "SCHEMA_SQL", "SCHEMA_VERSION"]

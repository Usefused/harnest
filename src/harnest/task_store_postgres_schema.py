"""Schema and atomic state transitions for PostgreSQL durable execution."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS harnest_durable_tasks (
    application_id text NOT NULL,
    job_id text NOT NULL,
    user_id text NOT NULL,
    task_name text NOT NULL,
    queue text NOT NULL,
    arguments jsonb NOT NULL,
    invocation jsonb,
    agent_permissions jsonb,
    trigger text NOT NULL,
    status text NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),
    scheduled_at double precision NOT NULL,
    max_retries integer NOT NULL CHECK(max_retries >= 0),
    attempt integer NOT NULL DEFAULT 0,
    lease_token text,
    lease_expires_at double precision,
    result jsonb,
    failure_code text,
    idempotency_key text,
    fingerprint text NOT NULL,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    PRIMARY KEY(application_id, job_id),
    UNIQUE(application_id, user_id, task_name, idempotency_key)
);
CREATE INDEX IF NOT EXISTS harnest_durable_tasks_ready
ON harnest_durable_tasks(application_id, queue, scheduled_at, job_id)
WHERE status='pending';
CREATE INDEX IF NOT EXISTS harnest_durable_tasks_expired
ON harnest_durable_tasks(application_id, lease_expires_at, job_id)
WHERE status='running';
CREATE TABLE IF NOT EXISTS harnest_durable_crons (
    application_id text NOT NULL,
    schedule_id text NOT NULL,
    user_id text NOT NULL,
    schedule_key text NOT NULL,
    expression text NOT NULL,
    timezone text NOT NULL CHECK(timezone='UTC'),
    task_name text NOT NULL,
    arguments jsonb NOT NULL,
    next_run_at double precision NOT NULL,
    status text NOT NULL CHECK(status IN ('active','paused','cancelled')),
    revision integer NOT NULL DEFAULT 0,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    PRIMARY KEY(application_id,schedule_id),
    UNIQUE(application_id,user_id,schedule_key)
);
CREATE INDEX IF NOT EXISTS harnest_durable_crons_due
ON harnest_durable_crons(application_id,next_run_at,schedule_id)
WHERE status='active';
"""

ENQUEUE_SQL = """
INSERT INTO harnest_durable_tasks (
    application_id,job_id,user_id,task_name,queue,arguments,invocation,
    agent_permissions,trigger,status,scheduled_at,max_retries,attempt,
    lease_token,lease_expires_at,result,failure_code,idempotency_key,
    fingerprint,created_at,updated_at
) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10,$11,$12,$13,
          $14,$15,$16::jsonb,$17,$18,$19,$20,$21)
ON CONFLICT DO NOTHING
RETURNING *
"""

EXPIRE_SQL = """
WITH exhausted AS (
    SELECT application_id,job_id FROM harnest_durable_tasks
    WHERE application_id=$1 AND queue=ANY($2::text[]) AND status='running'
      AND lease_expires_at<=$3 AND attempt>=max_retries+1
    ORDER BY lease_expires_at,job_id
    FOR UPDATE SKIP LOCKED LIMIT $4
)
UPDATE harnest_durable_tasks AS jobs
SET status='failed',failure_code='task_failed',result=NULL,arguments='{}'::jsonb,
    invocation=NULL,agent_permissions=NULL,lease_token=NULL,
    lease_expires_at=NULL,updated_at=$3
FROM exhausted
WHERE jobs.application_id=exhausted.application_id AND jobs.job_id=exhausted.job_id
"""

CLAIM_SQL = """
WITH ready AS (
    SELECT application_id,job_id FROM harnest_durable_tasks
    WHERE application_id=$1 AND queue=ANY($2::text[]) AND attempt<max_retries+1
      AND ((status='pending' AND scheduled_at<=$3)
        OR (status='running' AND lease_expires_at<=$3))
    ORDER BY scheduled_at,job_id
    FOR UPDATE SKIP LOCKED LIMIT $4
), claimed AS (
    UPDATE harnest_durable_tasks AS jobs
    SET status='running',attempt=attempt+1,lease_token=$5 || ':' || jobs.job_id,
        lease_expires_at=$3+$6,updated_at=$3
    FROM ready
    WHERE jobs.application_id=ready.application_id AND jobs.job_id=ready.job_id
    RETURNING jobs.*
)
SELECT * FROM claimed ORDER BY scheduled_at,job_id
"""

FINISH_SQL = """
UPDATE harnest_durable_tasks
SET status=CASE WHEN $5='pending' AND attempt>=max_retries+1 THEN 'failed' ELSE $5 END,
    result=CASE WHEN $5!='pending' THEN $6::jsonb
                WHEN attempt>=max_retries+1 THEN NULL ELSE result END,
    failure_code=CASE WHEN $5!='pending' THEN $7
                      WHEN attempt>=max_retries+1 THEN 'task_failed' ELSE failure_code END,
    scheduled_at=CASE WHEN $5='pending' AND attempt<max_retries+1 THEN $8 ELSE scheduled_at END,
    updated_at=$4,
    lease_token=NULL,lease_expires_at=NULL,
    arguments=CASE WHEN $5='pending' AND attempt<max_retries+1 THEN arguments ELSE '{}'::jsonb END,
    invocation=CASE WHEN $5='pending' AND attempt<max_retries+1 THEN invocation ELSE NULL END,
    agent_permissions=CASE WHEN $5='pending' AND attempt<max_retries+1 THEN agent_permissions ELSE NULL END
WHERE application_id=$1 AND job_id=$2 AND lease_token=$3
  AND status='running' AND lease_expires_at>$4
RETURNING job_id
"""

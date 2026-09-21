"""Schema and atomic state transitions for PostgreSQL durable channel storage."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS harnest_channel_events (
    platform text NOT NULL,
    installation_id text NOT NULL,
    provider_event_id text NOT NULL,
    delivery_id text NOT NULL,
    kind text NOT NULL,
    sender_id text NOT NULL,
    conversation_id text NOT NULL,
    thread_id text,
    message_id text NOT NULL,
    occurred_at double precision NOT NULL,
    content text NOT NULL,
    reply_to jsonb NOT NULL,
    PRIMARY KEY(platform, installation_id, provider_event_id)
);
CREATE TABLE IF NOT EXISTS harnest_channel_sessions (
    platform text NOT NULL,
    installation_id text NOT NULL,
    conversation_id text NOT NULL,
    thread_id text NOT NULL DEFAULT '',
    session_id text NOT NULL,
    PRIMARY KEY(platform, installation_id, conversation_id, thread_id)
);
CREATE TABLE IF NOT EXISTS harnest_channel_replies (
    reply_id text PRIMARY KEY,
    platform text NOT NULL,
    installation_id text NOT NULL,
    conversation_id text NOT NULL,
    reply_to jsonb NOT NULL,
    content text NOT NULL,
    status text NOT NULL CHECK(status IN ('pending','sent','failed','delivery_unknown')),
    receipt jsonb,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS harnest_channel_replies_pending
ON harnest_channel_replies(platform, installation_id, created_at, reply_id)
WHERE status='pending';
"""

ADMIT_EVENT_SQL = """
INSERT INTO harnest_channel_events (
    platform,installation_id,provider_event_id,delivery_id,kind,sender_id,
    conversation_id,thread_id,message_id,occurred_at,content,reply_to
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)
ON CONFLICT DO NOTHING
RETURNING *
"""

BIND_SESSION_SQL = """
INSERT INTO harnest_channel_sessions (
    platform,installation_id,conversation_id,thread_id,session_id
) VALUES ($1,$2,$3,$4,$5)
ON CONFLICT DO NOTHING
"""

ENQUEUE_REPLY_SQL = """
INSERT INTO harnest_channel_replies (
    reply_id,platform,installation_id,conversation_id,reply_to,content,
    status,receipt,created_at,updated_at
) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8::jsonb,$9,$10)
ON CONFLICT (reply_id) DO UPDATE SET
    platform=excluded.platform, installation_id=excluded.installation_id,
    conversation_id=excluded.conversation_id, reply_to=excluded.reply_to,
    content=excluded.content, status=excluded.status, receipt=excluded.receipt,
    created_at=excluded.created_at, updated_at=excluded.updated_at
RETURNING *
"""

FINISH_REPLY_SQL = """
UPDATE harnest_channel_replies
SET status=$2, receipt=$3::jsonb
WHERE reply_id=$1
RETURNING reply_id
"""

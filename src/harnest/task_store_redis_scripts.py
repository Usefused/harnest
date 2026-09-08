"""Atomic, bounded Redis operations for durable tasks and cron schedules."""

# Payload-bearing fields are JSON strings inside the record. Lua may mutate
# scheduling metadata without changing empty arrays, objects, or large numbers.
_TASK_HELPERS = """
local function scrub(job)
  job.arguments = '{}'
  job.invocation = 'null'
  job.agent_permissions = 'null'
  job.lease_token = cjson.null
  job.lease_expires_at = cjson.null
end
local function enqueue(raw, identity, fingerprint, ready)
  local incoming = cjson.decode(raw)
  local existing = redis.call('HGET', KEYS[1], incoming.job_id)
  if existing then
    local job = cjson.decode(existing)
    if job.user_id ~= incoming.user_id or job._fingerprint ~= fingerprint then
      return {'conflict'}
    end
  end
  if identity ~= '' then
    local id = redis.call('HGET', KEYS[2], identity)
    if id then existing = redis.call('HGET', KEYS[1], id) end
  end
  if existing then
    local job = cjson.decode(existing)
    if job.user_id ~= incoming.user_id or job._fingerprint ~= fingerprint then
      return {'conflict'}
    end
    return {'ok', existing}
  end
  incoming._fingerprint = fingerprint
  local encoded = cjson.encode(incoming)
  redis.call('HSET', KEYS[1], incoming.job_id, encoded)
  if identity ~= '' then redis.call('HSET', KEYS[2], identity, incoming.job_id) end
  redis.call('ZADD', ready, incoming.scheduled_at, incoming.job_id)
  return {'ok', encoded}
end
"""

ENQUEUE = _TASK_HELPERS + """
return enqueue(ARGV[1], ARGV[2], ARGV[3], KEYS[3])
"""

CLAIM = _TASK_HELPERS + """
local now, expiry, limit = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local candidates, claimed = {}, {}
for index=2,#KEYS,2 do
  local ready, leased = KEYS[index], KEYS[index+1]
  local expired = redis.call('ZRANGEBYSCORE', leased, '-inf', now, 'LIMIT', 0, limit)
  for _, id in ipairs(expired) do
    local raw = redis.call('HGET', KEYS[1], id)
    if raw then
      local job = cjson.decode(raw)
      redis.call('ZREM', leased, id)
      if job.attempt >= job.max_retries + 1 then
        job.status = 'failed'
        job.failure_code = 'task_failed'
        job.updated_at = now
        scrub(job)
        redis.call('HSET', KEYS[1], id, cjson.encode(job))
      else
        redis.call('ZADD', ready, job.scheduled_at, id)
      end
    end
  end
  local ids = redis.call('ZRANGEBYSCORE', ready, '-inf', now, 'LIMIT', 0, limit)
  for _, id in ipairs(ids) do
    local job = cjson.decode(redis.call('HGET', KEYS[1], id))
    table.insert(candidates, {job=job, ready=ready, leased=leased})
  end
end
table.sort(candidates, function(a,b)
  if a.job.scheduled_at == b.job.scheduled_at then return a.job.job_id < b.job.job_id end
  return a.job.scheduled_at < b.job.scheduled_at
end)
for index=1,math.min(limit,#candidates) do
    local selected = candidates[index]
    local job, ready, leased = selected.job, selected.ready, selected.leased
    local id = job.job_id
    job.status = 'running'
    job.attempt = job.attempt + 1
    job.lease_token = ARGV[4] .. ':' .. id .. ':' .. job.attempt
    job.lease_expires_at = expiry
    job.updated_at = now
    local encoded = cjson.encode(job)
    redis.call('HSET', KEYS[1], id, encoded)
    redis.call('ZREM', ready, id)
    redis.call('ZADD', leased, expiry, id)
    table.insert(claimed, encoded)
end
return claimed
"""

_OWNED_TASK = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
local job = cjson.decode(raw)
if job.status ~= 'running' or job.lease_token ~= ARGV[2]
   or job.lease_expires_at <= tonumber(ARGV[3]) then return 0 end
"""

RENEW = _OWNED_TASK + """
job.lease_expires_at = tonumber(ARGV[4])
job.updated_at = tonumber(ARGV[3])
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(job))
redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
return 1
"""

FINISH = _TASK_HELPERS + _OWNED_TASK + """
job.status = ARGV[4]
job.result = ARGV[5]
job.failure_code = cjson.decode(ARGV[6])
if job.status == 'pending' and job.attempt >= job.max_retries + 1 then
  job.status = 'failed'
  job.result = 'null'
  job.failure_code = 'task_failed'
end
job.updated_at = tonumber(ARGV[3])
job.lease_token = cjson.null
job.lease_expires_at = cjson.null
redis.call('ZREM', KEYS[3], ARGV[1])
if job.status == 'pending' then
  job.scheduled_at = tonumber(ARGV[7])
  redis.call('ZADD', KEYS[2], ARGV[7], ARGV[1])
else
  scrub(job)
end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(job))
return 1
"""

CANCEL = _TASK_HELPERS + """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
local job = cjson.decode(raw)
if job.user_id ~= ARGV[2] then return 0 end
if job.status ~= 'pending' and job.status ~= 'running' then return 0 end
job.status = 'cancelled'
job.result = 'null'
job.failure_code = 'task_cancelled'
job.updated_at = tonumber(ARGV[3])
scrub(job)
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(job))
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return 1
"""

CREATE_CRON = """
local incoming = cjson.decode(ARGV[1])
local collision = redis.call('HGET', KEYS[1], incoming.schedule_id)
if collision then
  local record = cjson.decode(collision)
  if record.user_id ~= incoming.user_id or record.key ~= incoming.key
     or record._fingerprint ~= ARGV[3] then return {'conflict'} end
end
local id = redis.call('HGET', KEYS[2], ARGV[2]) or incoming.schedule_id
local existing = redis.call('HGET', KEYS[1], id)
if existing then
  local record = cjson.decode(existing)
  if record.user_id ~= incoming.user_id or record.key ~= incoming.key
     or record._fingerprint ~= ARGV[3] then return {'conflict'} end
  return {'ok', existing}
end
incoming._fingerprint = ARGV[3]
incoming._due_member = ARGV[4]
local encoded = cjson.encode(incoming)
redis.call('HSET', KEYS[1], id, encoded)
redis.call('HSET', KEYS[2], ARGV[2], id)
redis.call('ZADD', KEYS[3], 0, id)
if incoming.status == 'active' then redis.call('ZADD', KEYS[4], 0, ARGV[4]) end
return {'ok', encoded}
"""

UPDATE_CRON = """
local incoming = cjson.decode(ARGV[1])
local raw = redis.call('HGET', KEYS[1], incoming.schedule_id)
if not raw then return {'missing'} end
local record = cjson.decode(raw)
if record.user_id ~= incoming.user_id then return {'missing'} end
if record.key ~= incoming.key or record.task_name ~= incoming.task_name
   or record.revision ~= tonumber(ARGV[2]) then return {'conflict'} end
if record.status == 'cancelled' then
  for key, value in pairs(incoming) do
    if record[key] ~= value then return {'conflict'} end
  end
  return {'ok', raw}
end
incoming.revision = record.revision + 1
incoming._fingerprint = ARGV[3]
incoming._due_member = ARGV[4]
redis.call('ZREM', KEYS[2], record._due_member)
if incoming.status == 'active' then redis.call('ZADD', KEYS[2], 0, ARGV[4]) end
local encoded = cjson.encode(incoming)
redis.call('HSET', KEYS[1], incoming.schedule_id, encoded)
return {'ok', encoded}
"""

DELETE_CRON = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
local record = cjson.decode(raw)
if record.user_id ~= ARGV[2] then return 0 end
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[3])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('ZREM', KEYS[4], record._due_member)
return 1
"""

LIST_CRONS = """
local ids = redis.call('ZRANGEBYLEX', KEYS[2], ARGV[1], ARGV[2], 'LIMIT', 0, ARGV[3])
local records = {}
for _, id in ipairs(ids) do
  if ARGV[4] == 'due' then id = string.sub(id, 18) end
  table.insert(records, redis.call('HGET', KEYS[1], id))
end
return records
"""

COMMIT_OCCURRENCE = _TASK_HELPERS + """
local raw = redis.call('HGET', KEYS[4], ARGV[1])
if not raw then return {'stale'} end
local record = cjson.decode(raw)
if record.user_id ~= ARGV[2] or record.status ~= 'active'
   or record.revision ~= tonumber(ARGV[3])
   or record.next_run_at ~= tonumber(ARGV[4]) then return {'stale'} end
local task = cjson.decode(ARGV[7])
if task.user_id ~= record.user_id or task.task_name ~= record.task_name
   or task.arguments ~= record.arguments then
  return {'cron-conflict'}
end
local result = enqueue(ARGV[7], ARGV[8], ARGV[9], KEYS[3])
if result[1] ~= 'ok' then return result end
redis.call('ZREM', KEYS[5], record._due_member)
record.next_run_at = tonumber(ARGV[5])
record.updated_at = task.created_at
record.revision = record.revision + 1
record._due_member = ARGV[6]
redis.call('HSET', KEYS[4], ARGV[1], cjson.encode(record))
redis.call('ZADD', KEYS[5], 0, ARGV[6])
return result
"""

READ_SCOPED = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return false end
if ARGV[3] ~= 'trusted' and cjson.decode(raw).user_id ~= ARGV[2] then return false end
return raw
"""

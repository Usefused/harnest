"""Atomic Lua operations used by the built-in Redis store."""

CREATE_SESSION = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], 0, ARGV[2])
if tonumber(ARGV[3]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[3])
  redis.call('EXPIRE', KEYS[2], ARGV[3])
end
return 1
"""

UPDATE_SESSION = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local record = cjson.decode(raw)
local delta = cjson.decode(ARGV[1])
for key, value in pairs(delta) do record['state'][key] = value end
record['updated_at'] = ARGV[2]
local encoded = cjson.encode(record)
redis.call('SET', KEYS[1], encoded)
if tonumber(ARGV[3]) > 0 then redis.call('EXPIRE', KEYS[1], ARGV[3]) end
if tonumber(ARGV[3]) > 0 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end
return encoded
"""

DELETE_SESSION = """
if redis.call('DEL', KEYS[1]) == 0 then return 0 end
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""

LEASE_REPLACE = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return false end
if redis.call('EXISTS', KEYS[1]) == 0 then return false end
redis.call('SET', KEYS[1], ARGV[2])
if tonumber(ARGV[3]) > 0 then redis.call('EXPIRE', KEYS[1], ARGV[3]) end
if tonumber(ARGV[3]) > 0 then redis.call('EXPIRE', KEYS[3], ARGV[3]) end
return ARGV[2]
"""

COMPARE_EXPIRE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('PEXPIRE', KEYS[1], ARGV[2])
"""

COMPARE_DELETE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""

BEGIN_RUN = """
local existing = redis.call('GET', KEYS[1])
if existing then return existing end
if redis.call('EXISTS', KEYS[2]) == 1 then return false end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('SET', KEYS[2], ARGV[2])
if tonumber(ARGV[3]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[3])
  redis.call('EXPIRE', KEYS[2], ARGV[3])
end
return ARGV[1]
"""

PUT_CHECKPOINT = """
local run_raw = redis.call('GET', KEYS[1])
if not run_raw then return {'missing'} end
local run = cjson.decode(run_raw)
if run['status'] ~= 'running' and run['status'] ~= 'waiting' then
  return {'terminal'}
end
local current_raw = redis.call('GET', KEYS[2])
local current_revision = -1
if current_raw then current_revision = cjson.decode(current_raw)['revision'] end
if current_revision ~= tonumber(ARGV[1]) then return {'conflict'} end
local record = cjson.decode(ARGV[2])
local sequence = redis.call('INCR', KEYS[7])
record['revision'] = sequence - 1
local encoded = cjson.encode(record)
redis.call('SET', KEYS[2], encoded)
redis.call('ZADD', KEYS[3], sequence, KEYS[2])
redis.call('ZADD', KEYS[4], sequence, KEYS[2])
redis.call('HSET', KEYS[5], ARGV[3], sequence)
redis.call('HSET', KEYS[6], ARGV[3], sequence)
if tonumber(ARGV[4]) > 0 then
  for index=2,7 do redis.call('EXPIRE', KEYS[index], ARGV[4]) end
end
return {'ok', encoded}
"""

PUT_WRITES = """
local run_raw = redis.call('GET', KEYS[1])
if not run_raw then return 'missing' end
local run = cjson.decode(run_raw)
if run['status'] ~= 'running' and run['status'] ~= 'waiting' then
  return 'terminal'
end
for index=1,#ARGV-1,2 do
  redis.call('HSETNX', KEYS[2], ARGV[index], ARGV[index+1])
end
local ttl = tonumber(ARGV[#ARGV])
if ttl > 0 then redis.call('EXPIRE', KEYS[2], ttl) end
return 'ok'
"""

GET_WRITES_BATCH = """
local result = {}
for index, key in ipairs(KEYS) do
  table.insert(result, ARGV[index])
  table.insert(result, cjson.encode(redis.call('HVALS', key)))
end
return result
"""

TRANSITION_RUN = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {'missing'} end
local run = cjson.decode(raw)
if run['status'] ~= ARGV[1] then return {'conflict'} end
run['status'] = ARGV[2]
run['pending_action'] = cjson.decode(ARGV[3])
run['revision'] = run['revision'] + 1
run['updated_at'] = ARGV[4]
local encoded = cjson.encode(run)
redis.call('SET', KEYS[1], encoded)
if ARGV[2] == 'completed' or ARGV[2] == 'failed' or ARGV[2] == 'cancelled' then
  if redis.call('GET', KEYS[2]) == ARGV[5] then redis.call('DEL', KEYS[2]) end
end
if tonumber(ARGV[6]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[6])
  if redis.call('GET', KEYS[2]) == ARGV[5] then
    redis.call('EXPIRE', KEYS[2], ARGV[6])
  end
end
return {'ok', encoded}
"""

SUSPEND_CONTINUATION = """
local run_raw = redis.call('GET', KEYS[1])
if not run_raw then return {'missing'} end
local run = cjson.decode(run_raw)
local continuation = cjson.decode(ARGV[1])['record']
if run['status'] ~= 'running'
   or run['application_id'] ~= continuation['application_id']
   or run['user_id'] ~= continuation['user_id']
   or run['session_id'] ~= continuation['session_id']
   or run['run_id'] ~= continuation['run_id'] then
  return {'conflict'}
end
if redis.call('EXISTS', KEYS[2]) == 1 or redis.call('EXISTS', KEYS[3]) == 1 then
  return {'conflict'}
end
redis.call('SET', KEYS[2], ARGV[1])
redis.call('SET', KEYS[3], ARGV[2])
redis.call('ZADD', KEYS[4], 0, ARGV[2])
run['status'] = 'waiting'
run['pending_action'] = cjson.decode(ARGV[3])
run['revision'] = run['revision'] + 1
run['updated_at'] = ARGV[4]
local encoded_run = cjson.encode(run)
redis.call('SET', KEYS[1], encoded_run)
if tonumber(ARGV[5]) > 0 then
  for index=1,4 do redis.call('EXPIRE', KEYS[index], ARGV[5]) end
end
return {'ok', ARGV[1]}
"""

RESOLVE_CONTINUATION = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return {'missing'} end
local raw = redis.call('GET', KEYS[1])
local run_raw = redis.call('GET', KEYS[4])
if not raw or not run_raw then return {'missing'} end
local envelope = cjson.decode(raw)
local record = envelope['record']
local run = cjson.decode(run_raw)
local action = run['pending_action']
if record['application_id'] ~= ARGV[2] or record['user_id'] ~= ARGV[3]
   or record['session_id'] ~= ARGV[4] or record['run_id'] ~= ARGV[5]
   or record['provider'] ~= ARGV[6] or record['schema_id'] ~= ARGV[7]
   or record['status'] ~= 'pending' or run['status'] ~= 'waiting'
   or not action or action['action_id'] ~= record['continuation_id'] then
  return {'conflict'}
end
record['status'] = ARGV[8]
record['result'] = cjson.decode(ARGV[9])
record['failure'] = cjson.decode(ARGV[10])
record['revision'] = record['revision'] + 1
record['updated_at'] = ARGV[11]
local encoded = cjson.encode(envelope)
redis.call('SET', KEYS[1], encoded)
redis.call('ZREM', KEYS[3], ARGV[1])
if tonumber(ARGV[12]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[12])
  redis.call('EXPIRE', KEYS[2], ARGV[12])
end
return {'ok', encoded}
"""

CLAIM_CONTINUATION = """
local continuation_raw = redis.call('GET', KEYS[1])
local run_raw = redis.call('GET', KEYS[2])
if not continuation_raw or not run_raw then return {'missing'} end
local envelope = cjson.decode(continuation_raw)
local record = envelope['record']
local run = cjson.decode(run_raw)
local action = run['pending_action']
local resolved = record['status'] == 'completed' or record['status'] == 'failed'
if record['application_id'] ~= ARGV[1] or record['user_id'] ~= ARGV[2]
   or record['session_id'] ~= ARGV[3] or record['run_id'] ~= ARGV[4]
   or record['provider'] ~= ARGV[5] or record['revision'] ~= tonumber(ARGV[6])
   or not resolved or record['ready'] ~= true
   or run['status'] ~= 'waiting' or not action
   or action['action_id'] ~= record['continuation_id'] then
  return {'conflict'}
end
record['status'] = 'claimed'
record['revision'] = record['revision'] + 1
record['updated_at'] = ARGV[7]
run['status'] = 'running'
run['pending_action'] = cjson.decode('null')
run['revision'] = run['revision'] + 1
run['updated_at'] = ARGV[7]
local encoded = cjson.encode(envelope)
redis.call('SET', KEYS[1], encoded)
redis.call('SET', KEYS[2], cjson.encode(run))
if tonumber(ARGV[8]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[8])
  redis.call('EXPIRE', KEYS[2], ARGV[8])
end
return {'ok', encoded}
"""

ARM_CONTINUATION = """
local raw = redis.call('GET', KEYS[1])
if not raw then return {'missing'} end
local envelope = cjson.decode(raw)
local record = envelope['record']
local armable = record['status'] == 'pending'
  or record['status'] == 'completed' or record['status'] == 'failed'
if record['application_id'] ~= ARGV[1] or record['user_id'] ~= ARGV[2]
   or record['session_id'] ~= ARGV[3] or record['run_id'] ~= ARGV[4]
   or record['provider'] ~= ARGV[5] or record['revision'] ~= tonumber(ARGV[6])
   or record['ready'] == true or not armable then
  return {'conflict'}
end
record['ready'] = true
record['revision'] = record['revision'] + 1
record['updated_at'] = ARGV[7]
local encoded = cjson.encode(envelope)
redis.call('SET', KEYS[1], encoded)
if tonumber(ARGV[8]) > 0 then redis.call('EXPIRE', KEYS[1], ARGV[8]) end
return {'ok', encoded}
"""

DELETE_RUN = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
redis.call('DEL', KEYS[1], KEYS[3], KEYS[4], KEYS[5])
if KEYS[6] then
  redis.call('DEL', KEYS[6], KEYS[7])
  redis.call('ZREM', KEYS[8], ARGV[2])
end
return raw
"""

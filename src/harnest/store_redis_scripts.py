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

DELETE_RUN = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
redis.call('DEL', KEYS[1], KEYS[3], KEYS[4], KEYS[5])
return raw
"""

"""Redis memory using atomic scripts and bounded, scope-local text scans."""

import hashlib
import json
import time
from dataclasses import replace
from typing import Any

from .memory import MemoryCleanupPage, MemoryConflictError, MemoryPage, MemoryRecord, MemoryScope
from .memory_provider import MemoryProvider, decode_record, encode_record, text_field

_LIVE = """
-- Scripts do not roll back command errors. Validate all types before mutation.
local records_type=redis.call('TYPE',KEYS[1]).ok
local keys_type=redis.call('TYPE',KEYS[2]).ok
local namespaces_type=redis.call('TYPE',KEYS[3]).ok
if records_type~='none' and records_type~='hash' then error('invalid memory records type') end
if keys_type~='none' and keys_type~='zset' then error('invalid memory index type') end
if namespaces_type~='none' and namespaces_type~='zset' then error('invalid memory namespace index type') end
local function live(key)
 local raw=redis.call('HGET',KEYS[1],key)
 if not raw then return nil end
 local value=cjson.decode(raw)
 if value.expires_at~=cjson.null and value.expires_at<=tonumber(ARGV[1]) then
  redis.call('HDEL',KEYS[1],key); redis.call('ZREM',KEYS[2],key)
  return nil
 end
 return value,raw
end
"""
_PUT = _LIVE + """
local value=cjson.decode(ARGV[2])
local current=live(value.key)
if ARGV[3]~='' and (not current or current.revision~=ARGV[3]) then return {'conflict'} end
-- Keep Python's JSON opaque: Lua CJSON changes arrays and numeric precision.
-- Python retries if another writer changes the creation timestamp we observed.
if current and current.revision~=ARGV[4] then return {'retry',select(2,live(value.key))} end
if not current and ARGV[4]~='' then return {'retry',''} end
local raw=ARGV[2]
redis.call('HSET',KEYS[1],value.key,raw)
redis.call('ZADD',KEYS[2],0,value.key)
redis.call('ZADD',KEYS[3],0,KEYS[1])
return {'ok',raw}
"""
_DELETE = _LIVE + """
local current=live(ARGV[2])
if ARGV[3]~='' and (not current or current.revision~=ARGV[3]) then return -1 end
redis.call('HDEL',KEYS[1],ARGV[2]); redis.call('ZREM',KEYS[2],ARGV[2])
if current then return 1 else return 0 end
"""
_READ = _LIVE + """
if ARGV[2]~='' then
 local value,raw=live(ARGV[2]); if value then return {'',raw} else return {''} end
end
local lower='-'; if ARGV[5]~='' then lower='('..ARGV[5] end
local keys=redis.call('ZRANGEBYLEX',KEYS[2],lower,'+','LIMIT',0,129)
local result={''}
for i=1,math.min(128,#keys) do
 local value,raw=live(keys[i])
 if value and (ARGV[3]=='' or string.find(value.content,ARGV[3],1,true)) then
  table.insert(result,raw)
 end
 if i<#keys then result[1]=keys[i] else result[1]='' end
 if #result-1>=tonumber(ARGV[4]) then break end
end
return result
"""
_CLEAR = _LIVE + """
local count=redis.call('HLEN',KEYS[1])
redis.call('UNLINK',KEYS[1],KEYS[2])
redis.call('ZREM',KEYS[3],KEYS[1])
return count
"""
_PURGE = _LIVE + """
local lower='-'; if ARGV[3]~='' then lower='('..ARGV[3] end
local limit=tonumber(ARGV[2])
local keys=redis.call('ZRANGEBYLEX',KEYS[2],lower,'+','LIMIT',0,limit+1)
local deleted=0
for i=1,math.min(limit,#keys) do
 local raw=redis.call('HGET',KEYS[1],keys[i])
 if raw and not live(keys[i]) then deleted=deleted+1 end
end
local cursor=''; if #keys>limit then cursor=keys[limit] end
return {deleted,cursor}
"""
_DELETE_USER = _LIVE + """
local members=redis.call('ZRANGE',KEYS[3],0,99)
local count=0
local owner=string.sub(KEYS[3],1,-12)..':'
-- Validate the complete batch before mutation: Redis cannot roll back errors.
for _,key in ipairs(members) do
 if string.sub(key,1,#owner)~=owner or string.sub(key,-8)~=':records' then
  error('invalid memory namespace membership')
 end
 count=count+redis.call('HLEN',key)
end
for _,key in ipairs(members) do
 redis.call('UNLINK',key,string.sub(key,1,-9)..':keys')
 redis.call('ZREM',KEYS[3],key)
end
return {count,redis.call('ZCARD',KEYS[3])}
"""


class RedisMemoryStore(MemoryProvider):
    """Store explicit memories in Redis; durability requires persistent Redis."""

    def __init__(self, url: str, *, prefix: str = "harnest-memory") -> None:
        """Retain connection settings without creating a client at import time."""
        self._url = url
        self._prefix = text_field(prefix, "prefix", 128)
        self._client: Any = None

    async def start(self) -> None:
        """Open an owned async client only when the selected provider starts."""
        if self._client is not None:
            return
        import redis.asyncio
        from redis.backoff import NoBackoff
        from redis.asyncio.retry import Retry
        # A lost write acknowledgement is ambiguous; automatic replay can replace
        # a committed revision and conceal that uncertainty from the caller.
        self._client = redis.asyncio.from_url(self._url, decode_responses=True, retry=Retry(NoBackoff(), 0))
        try:
            await self._client.ping()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Release only this provider's Redis client."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _eval(self, scope: MemoryScope, script: str, *args: Any) -> Any:
        """Co-locate one user's namespaces so maintenance never scans global keys."""
        if self._client is None:
            raise RuntimeError("memory provider is not started")
        identity = json.dumps([scope.application_id, scope.user_id])
        digest = hashlib.sha256(identity.encode()).hexdigest()
        owner = f"{self._prefix}:{{{digest}}}"
        namespace = hashlib.sha256(scope.namespace.encode()).hexdigest()
        base = f"{owner}:{namespace}"
        return await self._client.eval(script, 3, base + ":records", base + ":keys", owner + ":namespaces", time.time(), *args)

    async def _put(self, record: MemoryRecord, expected: str | None) -> MemoryRecord:
        """Preserve opaque JSON while fencing timestamp preservation across writers."""
        observed = ""
        original = record
        for _ in range(32):
            response = await self._eval(record.scope, _PUT, encode_record(record), expected or "", observed)
            if response[0] == "conflict":
                raise MemoryConflictError("memory revision does not match")
            if response[0] == "ok":
                return decode_record(response[1])
            current = decode_record(response[1]) if response[1] else None
            observed = current.revision if current else ""
            record = replace(original, created_at=current.created_at) if current else original
        raise RuntimeError("memory write contention exceeded retry limit")

    async def _read(self, scope: MemoryScope, key: str | None, query: str | None, limit: int, after: str | None) -> MemoryPage:
        """Bound each script to 128 candidates, returning a cursor even without matches."""
        response = await self._eval(scope, _READ, key or "", query or "", limit, after or "")
        return MemoryPage(tuple(decode_record(raw) for raw in response[1:]), response[0] or None)

    async def _delete(self, scope: MemoryScope, key: str, expected: str | None) -> bool:
        """Remove both stored content and search membership in one script."""
        result = await self._eval(scope, _DELETE, key, expected or "")
        if result == -1:
            raise MemoryConflictError("memory revision does not match")
        return bool(result)

    async def _delete_all(self, scope: MemoryScope) -> int:
        """Unlink scoped data and indexes without blocking on payload frees."""
        return int(await self._eval(scope, _CLEAR))

    async def _delete_user(self, scope: MemoryScope) -> int:
        """Erase bounded namespace batches; callers must first revoke new writes."""
        total = 0
        for _ in range(1024):
            count, remaining = await self._eval(scope, _DELETE_USER)
            total += int(count)
            if not remaining:
                return total
        raise RuntimeError("memory cleanup exceeded batch limit; retry remaining namespaces")

    async def _purge_expired(self, scope: MemoryScope, limit: int, after: str | None) -> MemoryCleanupPage:
        """Sweep ordered keys in a bounded atomic script without touching live data."""
        response = await self._eval(scope, _PURGE, limit, after or "")
        return MemoryCleanupPage(int(response[0]), response[1] or None)

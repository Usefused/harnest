from harnest.store import MemoryStore


# The example keeps one development store for committed sessions and private
# in-progress checkpoints so both framework adapters see the same authority.
store = MemoryStore()

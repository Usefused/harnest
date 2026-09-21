"""Share one durable session/checkpoint provider within the compiled agent."""

import os

from harnest.store import PostgresStore

store = PostgresStore(os.environ["CHANNEL_DATABASE_URL"])

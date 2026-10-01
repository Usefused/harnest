"""Current runtime authoring starters, validated without importing project code."""

import json

from fastapi import HTTPException

from harnest.cron import cron


def cron_source(identity: str, options: dict) -> dict[str, str]:
    """Share schedule and queue validation with the runtime's public decorator."""
    from .catalog import resource_name

    mode = options.get("mode", "existing" if options.get("task") else "fixed")
    if mode not in {"fixed", "dynamic", "existing"}:
        raise HTTPException(422, "Choose a fixed, dynamic, or existing-task schedule.")
    schedule = None if mode == "dynamic" else options.get("schedule", "0 9 * * *")
    arguments = None if mode == "dynamic" else _arguments(options.get("arguments", '{"payload": "scheduled"}'))
    queue = options.get("queue", "default")
    try:
        retries = int(options.get("max_retries", "3"))
        # A local probe validates runtime policy without executing authored code.
        cron(schedule, arguments=arguments, queue=queue, max_retries=retries)(_cron_probe)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, str(error)) from error
    if mode == "existing":
        task = resource_name(options.get("task", ""))
        source = f"from harnest.cron import Cron\nfrom tasks.{task} import {task}\n\n{identity} = Cron(schedule={schedule!r}, task={task}, arguments={arguments!r})\n"
    else:
        fixed = "" if mode == "dynamic" else f"{schedule!r}, arguments={arguments!r}, "
        source = f'''from harnest.cron import cron


@cron({fixed}queue={queue!r}, max_retries={retries})
async def {identity}(**arguments):
    """Replace this starter with the scheduled operation; make side effects idempotent."""
    return arguments
'''
    return {f"cron/{identity}.py": source}


def _cron_probe(**arguments):
    """Accept JSON arguments solely for declaration validation."""
    return arguments


def _arguments(value: str) -> dict:
    """Require an object; the runtime also rejects non-finite and unsafe values."""
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as error:
        raise HTTPException(422, "Schedule arguments must be a JSON object.") from error
    if not isinstance(result, dict):
        raise HTTPException(422, "Schedule arguments must be a JSON object.")
    return result


def task_storage_source(identity: str, options: dict) -> dict[str, str]:
    """Assign task and cron roles to one provider so leases share an authority."""
    providers = {
        "memory": ("from harnest.task import MemoryTaskStore", "MemoryTaskStore()", "Local development only: state is lost on restart."),
        "postgres": ("import os\nfrom harnest.task import PostgresTaskStore", 'PostgresTaskStore(os.environ["DATABASE_URL"])', "Run harnest env sync and supply DATABASE_URL at runtime."),
        "redis": ("import os\nfrom harnest_redis import RedisStore", 'RedisStore(os.environ["REDIS_URL"])', "Run harnest env sync; configure Redis persistence and supply REDIS_URL."),
    }
    provider = providers.get(options.get("provider", "memory"))
    if provider is None:
        raise HTTPException(422, "Choose memory, postgres, or redis storage.")
    imports, factory, note = provider
    return {f"lifecycle/{identity}.py": f'''from harnest import lifecycle
{imports}


# {note}
# Replace existing task/cron providers; each role must have exactly one owner.
@lifecycle.storage.tasks
@lifecycle.storage.cron
def {identity}():
    """Share one store instance for queued tasks and scheduled dispatch."""
    return {factory}
'''}


def client_input_source(identity: str, _options: dict) -> dict[str, str]:
    """Scaffold a private boundary that fails until its consuming handler is implemented."""
    return {f"tools/{identity}.py": f'''from pydantic import BaseModel
from harnest.agent import client_input


class PrivateInput(BaseModel):
    """Describe the private form fields without putting secrets in schema defaults."""
    value: str


@client_input(input_schema=PrivateInput, response={{"status": "accepted"}})
async def {identity}(private: PrivateInput) -> None:
    """Consume input privately; only the authored response reaches the model."""
    # Use private.value here. Never log, return, or persist the raw input in state.
    raise NotImplementedError("Implement the private input handler before serving.")
'''}


RUNTIME_TEMPLATES = {"cron": cron_source, "task-storage": task_storage_source, "client-input": client_input_source}

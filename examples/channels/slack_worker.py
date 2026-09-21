"""Run a durable Slack channel with an installed, immutable generated Fused SDK."""

import argparse
import asyncio
from functools import partial
import importlib
import json
import os
from pathlib import Path
import signal

import httpx

from harnest.channels import ChannelBinding, ChannelHTTPInvoker, ChannelWorker
from harnest.task import PostgresTaskStore
from harnest_fused import FusedChannelReceiver, FusedSlackReplySender, slack_mention


def required(name):
    """Load runtime secret inputs from the environment, never the reviewed JSON config."""
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"set {name} before starting the channel worker")
    return value


async def serve(config):
    """Start durable storage before intake and stop intake before closing dependencies."""
    sdk = importlib.import_module(config["sdk_module"])
    sdk_token, agent_token = required("FUSED_CHANNEL_TOKEN"), required("CHANNEL_AGENT_TOKEN")
    store = PostgresTaskStore(required("CHANNEL_DATABASE_URL"))
    await store.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        async with httpx.AsyncClient(base_url=config["agent_url"], timeout=120, trust_env=False) as agent, httpx.AsyncClient(
            base_url=config["engine_url"], headers={"Authorization": "Bearer " + sdk_token}, timeout=60, trust_env=False,
        ) as engine:
            worker = build_worker(config, store, agent, engine, agent_token)
            generated = sdk.FusedWebhooks.listen(config["receiver_name"], sdk_token, backend_url=config["grpc_url"])
            receiver = FusedChannelReceiver(generated, worker, partial(
                slack_mention, installation_id=config["slack_team_id"], app_id=config["slack_app_id"],
            ))
            try:
                receiver.start(config["webhook_event"])
                print("Channel worker started; waiting for allowlisted Slack mentions.", flush=True)
                await worker.run(stop)
            finally:
                receiver.close()
    finally:
        await store.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def build_worker(config, store, agent, engine, agent_token):
    """Separate authenticated actor identity from the fixed Fused bot connection."""
    invoke = ChannelHTTPInvoker(agent, lambda actor: {
        "Authorization": "Bearer " + agent_token, "X-Channel-Actor": actor,
    })
    send = FusedSlackReplySender(engine, app_id=config["app_id"], operation=config["reply_operation"],
                               selector={"end_user_ref": config["end_user_ref"]})
    binding = ChannelBinding("slack", "fused", (config["slack_team_id"],), tuple(config["allowed_conversations"]))
    return ChannelWorker(application_id=config["application_id"], binding_id=config["binding_id"],
                         binding=binding, store=store, invoke=invoke, send=send,
                         allowed_senders=tuple(config["allowed_senders"]), not_before=float(config["accept_after"]))


def main():
    """Require an explicit reviewed channel configuration instead of guessed provider IDs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    options = parser.parse_args()
    asyncio.run(serve(json.loads(options.config.read_text())))


if __name__ == "__main__":
    main()

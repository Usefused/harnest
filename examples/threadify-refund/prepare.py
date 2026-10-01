"""Seed an order and report payment verification from a separate service."""

import asyncio
import os
import sys

import httpx
from threadify import Threadify


async def main(order_id: str, owner: str) -> None:
    """Create one synthetic order and its prerequisite on the shared thread."""
    headers = {"Authorization": f"Bearer {os.environ['REFUND_API_TOKEN']}"}
    async with httpx.AsyncClient(base_url=os.environ["REFUND_API_URL"], timeout=5) as backend:
        response = await backend.post(
            "/demo/orders", headers=headers,
            json={"id": order_id, "owner": owner, "amount": 42, "currency": "GBP"},
        )
        response.raise_for_status()
    connection = await Threadify.connect(
        os.environ["THREADIFY_ORDERS_API_KEY"], service_name="orders",
        engine_url=os.environ["THREADIFY_ENGINE_URL"],
    )
    try:
        workflow = await connection.thread(
            f"refund:{order_id}", {"contract": "agent_refund:1", "role": "orders"},
        )
        await workflow.step("payment_verified").idempotency_key(
            f"payment:{order_id}"
        ).add_context({"reference": order_id}).success(wait_for=True)
        async with httpx.AsyncClient(base_url=os.environ["REFUND_API_URL"], timeout=5) as backend:
            response = await backend.put(
                f"/demo/orders/{order_id}/thread", headers=headers,
                json={"thread_id": workflow.thread_id},
            )
            response.raise_for_status()
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))

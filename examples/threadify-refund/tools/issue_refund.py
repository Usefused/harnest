"""A Harnest tool that waits for Threadify permission before issuing a refund."""

import os
from uuid import NAMESPACE_URL, uuid5

import httpx
from harnest import context
from harnest.agent import tool
from harnest.agent.approval import request_human_approval
from harnest_threadify import Threadify
from threadify import ThreadifyError, WaitOptions


def _headers(actor: str) -> dict[str, str]:
    """Bind the backend call to the Harnest principal and configured service key."""
    return {
        "Authorization": f"Bearer {os.environ['REFUND_API_TOKEN']}",
        "X-Actor": actor,
    }


async def _authorized_order(client: httpx.AsyncClient, order_id: str, actor: str) -> dict:
    """Get the trusted workflow key and amount after backend ownership checks."""
    response = await client.get(f"/orders/{order_id}", headers=_headers(actor))
    response.raise_for_status()
    return response.json()


async def _issue(client: httpx.AsyncClient, order: dict, actor: str, invocation_id: str) -> dict:
    """Use the same invocation as the payment idempotency key."""
    response = await client.post(
        "/refunds", headers={**_headers(actor), "Idempotency-Key": invocation_id},
        json={"order_id": order["id"], "amount": order["amount"], "currency": order["currency"]},
    )
    response.raise_for_status()
    return response.json()


def _permission_failure(error: Exception) -> dict[str, str]:
    """Keep Engine availability failures distinct from contract refusals."""
    if isinstance(error, ThreadifyError):
        decision = getattr(getattr(error, "decision", None), "decision", "")
        if decision == "unavailable" or error.code in {
            "THREADIFY_VALIDATION_UNAVAILABLE", "THREADIFY_WAIT_TIMEOUT",
        }:
            return {"status": "threadify_unavailable"}
    return {"status": "blocked"}


@tool
async def issue_refund(order_id: str) -> dict[str, str]:
    """Refund an owned order after human approval and a Threadify contract grant."""
    active = context.current()
    integration = context.resource("threadify", Threadify)
    base_url = os.environ["REFUND_API_URL"]
    async with httpx.AsyncClient(base_url=base_url, timeout=5) as backend:
        try:
            order = await _authorized_order(backend, order_id, active.user_id)
        except (httpx.HTTPError, ValueError):
            return {"status": "order_unavailable"}
        invocation_id = str(uuid5(NAMESPACE_URL, f"refund:{active.invocation_id}:{order['id']}"))
        message = f"Issue a {order['amount']} {order['currency']} refund for order {order['id']}?"
        async with request_human_approval(
            action="refund.issue", message=message,
            arguments={"order_id": order["id"], "amount": order["amount"], "currency": order["currency"]},
        ):
            try:
                workflow = await integration.join_workflow(order["thread_id"], role="processor")
                # Harnest's one-time approval is recorded before Threadify checks
                # the cross-service prerequisite for this exact refund attempt.
                await workflow.step("approval").idempotency_key(f"approval:{invocation_id}").add_context(
                    {"reference": order["id"]}
                ).success(wait_for=True)
                grant = await workflow.wait_for(
                    "refund_issued", WaitOptions(timeout=10, invocation_id=invocation_id),
                )
            except Exception as error:
                return _permission_failure(error)
            try:
                receipt = await _issue(backend, order, active.user_id, grant.invocation_id)
            except httpx.HTTPError:
                # A transport or backend failure may have committed payment.
                # Keep the claim for reconciliation; never retry here.
                return {"status": "outcome_unknown"}
            try:
                await workflow.step("refund_issued").idempotency_key(invocation_id).add_context(
                    {"reference": order["id"], "receipt": receipt["receipt"]}
                ).success(wait_for=True)
                await workflow.step("refund_complete").idempotency_key(
                    f"complete:{invocation_id}"
                ).success(wait_for=True)
            except Exception:
                # A committed refund must remain visible even when the evidence
                # report or its validation cannot be confirmed immediately.
                return {"status": "issued_unconfirmed", "receipt": receipt["receipt"]}
            return {"status": "issued", "receipt": receipt["receipt"]}

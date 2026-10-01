"""Local refund service used to exercise a real HTTP side effect in the example."""

import os
from threading import Lock

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


app = FastAPI(title="Local refund example backend")
_lock = Lock()
_orders: dict[str, dict] = {}
_receipts: dict[str, dict] = {}


class OrderInput(BaseModel):
    """Synthetic order supplied by the local setup script."""

    id: str
    owner: str
    amount: int
    currency: str = "GBP"


class RefundInput(BaseModel):
    """Refund request whose amount is checked against the stored order."""

    order_id: str
    amount: int
    currency: str


class ThreadBinding(BaseModel):
    """Trusted Threadify ID written after the order service validates its step."""

    thread_id: str


def _authorize(authorization: str) -> None:
    """Restrict even the local example to the configured service key."""
    expected = os.environ.get("REFUND_API_TOKEN")
    if not expected or authorization != f"Bearer {expected}":
        raise HTTPException(401, "unauthorized")


@app.post("/demo/orders")
def create_order(order: OrderInput, authorization: str = Header(default="")) -> dict:
    """Seed one synthetic paid order for a local workflow run."""
    _authorize(authorization)
    if order.amount <= 0 or not order.id or not order.owner:
        raise HTTPException(400, "invalid order")
    value = {**order.model_dump(), "workflow_key": f"refund:{order.id}", "thread_id": ""}
    with _lock:
        existing = _orders.get(order.id)
        if existing is not None and any(
            existing[key] != value[key] for key in value if key != "thread_id"
        ):
            raise HTTPException(409, "order already exists")
        if existing is None:
            _orders[order.id] = value
        return dict(_orders[order.id])


@app.put("/demo/orders/{order_id}/thread")
def bind_thread(
    order_id: str, binding: ThreadBinding, authorization: str = Header(default=""),
) -> dict:
    """Publish the Threadify ID only after prerequisite validation succeeds."""
    _authorize(authorization)
    if not binding.thread_id:
        raise HTTPException(400, "thread ID required")
    with _lock:
        order = _orders.get(order_id)
        if order is None:
            raise HTTPException(404, "order unavailable")
        if order["thread_id"] and order["thread_id"] != binding.thread_id:
            raise HTTPException(409, "order already bound to another thread")
        order["thread_id"] = binding.thread_id
        return dict(order)


@app.get("/orders/{order_id}")
def get_order(order_id: str, authorization: str = Header(default=""), x_actor: str = Header(default="")) -> dict:
    """Return the trusted business key only to the order's actor."""
    _authorize(authorization)
    with _lock:
        order = _orders.get(order_id)
        if order is None or order["owner"] != x_actor or not order["thread_id"]:
            raise HTTPException(404, "order unavailable")
        return dict(order)


@app.post("/refunds")
def refund(
    request: RefundInput, authorization: str = Header(default=""),
    x_actor: str = Header(default=""), idempotency_key: str = Header(default=""),
) -> dict:
    """Apply one refund per order and return the same receipt for a retry."""
    _authorize(authorization)
    if not idempotency_key:
        raise HTTPException(400, "idempotency key required")
    with _lock:
        order = _orders.get(request.order_id)
        if order is None or order["owner"] != x_actor:
            raise HTTPException(404, "order unavailable")
        if (request.amount, request.currency) != (order["amount"], order["currency"]):
            raise HTTPException(409, "refund differs from order")
        existing = _receipts.get(request.order_id)
        if existing is not None:
            if existing["idempotency_key"] != idempotency_key:
                raise HTTPException(409, "order already refunded")
            return {"receipt": existing["receipt"]}
        receipt = f"RF-{len(_receipts) + 1:08d}"
        _receipts[request.order_id] = {"idempotency_key": idempotency_key, "receipt": receipt}
        return {"receipt": receipt}

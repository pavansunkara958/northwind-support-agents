"""Order tools: status, cancellation, address change.

Every function begins with the same ownership check. It is repeated rather
than factored into a decorator deliberately: a reader auditing this file can
see, in each function body, that the order is checked against
`ctx.customer_id`. A decorator that could be forgotten on one new tool is a
worse trade than four duplicated lines.
"""
from __future__ import annotations

from core.types import RequestContext
from tools import backends
from tools.registry import tool


def _owned_order(ctx: RequestContext, order_id: str) -> tuple[dict | None, dict | None]:
    """Return (order, error). The order must belong to the AUTHENTICATED customer.

    Note what is NOT a parameter of any tool in this file: customer_id. The
    principal comes from the request context. A message claiming to be someone
    else changes nothing here.
    """
    order = backends.get_order(order_id)
    if order is None or order["customer_id"] != ctx.customer_id:
        # Identical response for "does not exist" and "belongs to someone else".
        # Distinguishing them turns this tool into an order-ID oracle.
        return None, {"error": "order_not_found",
                      "message": f"No order {order_id} on this account."}
    return order, None


@tool(risk="low", writes=False, agent="order",
      description="Look up the current status, carrier and tracking number of an order.")
def get_order_status(ctx: RequestContext, order_id: str) -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    return {
        "order_id": order["order_id"], "status": order["status"],
        "carrier": order["carrier"], "tracking": order["tracking"],
        "eta": order["eta"], "placed_at": order["placed_at"],
        "delivered_at": order["delivered_at"],
        "items": [{"name": i["name"], "qty": i["qty"]} for i in order["items"]],
    }


@tool(risk="medium", writes=True, agent="order",
      description="Cancel an order that has not yet been dispatched.")
def cancel_order(ctx: RequestContext, order_id: str) -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    if order["status"] not in ("processing", "pending"):
        return {"error": "not_cancellable",
                "message": f"Order {order['order_id']} is {order['status']} and can no "
                           f"longer be cancelled; a return can be started instead."}
    order["status"] = "cancelled"
    return {"order_id": order["order_id"], "status": "cancelled",
            "refund_due": order["total"], "currency": order["currency"]}


@tool(risk="medium", writes=True, agent="order",
      description="Change the delivery address of an order that has not yet been dispatched.")
def change_shipping_address(ctx: RequestContext, order_id: str, new_address: str) -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    if order["status"] != "processing":
        return {"error": "already_dispatched",
                "message": f"Order {order['order_id']} is {order['status']}; the carrier "
                           f"must be contacted directly to redirect it."}
    if len(new_address.strip()) < 8:
        return {"error": "invalid_address", "message": "That address looks incomplete."}
    order["address"] = new_address.strip()
    return {"order_id": order["order_id"], "address": order["address"], "status": order["status"]}

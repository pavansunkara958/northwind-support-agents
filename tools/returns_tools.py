"""Returns and refunds.

`refund_order` is the most dangerous function in this repository: it moves
money and cannot be undone by the agent. It is therefore the one place where
every control in the system is visible at once — ownership, business rules,
value threshold, and a signed human approval verified at the moment of
execution.
"""
from __future__ import annotations

from core import config
from core.types import RequestContext
from security import approvals, audit
from tools import backends
from tools.registry import tool

RETURN_WINDOW_DAYS = 30


def _owned_order(ctx: RequestContext, order_id: str):
    order = backends.get_order(order_id)
    if order is None or order["customer_id"] != ctx.customer_id:
        return None, {"error": "order_not_found",
                      "message": f"No order {order_id} on this account."}
    return order, None


@tool(risk="low", writes=True, agent="returns",
      description="Open a return for a delivered order within the 30-day returns window.")
def start_return(ctx: RequestContext, order_id: str, reason: str) -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    if order["status"] != "delivered":
        return {"error": "not_delivered",
                "message": f"Order {order['order_id']} is {order['status']}; a return can "
                           f"only be opened once it has been delivered."}
    days = backends.days_since_delivery(order) or 0
    if days > RETURN_WINDOW_DAYS:
        return {"error": "outside_window",
                "message": f"That order was delivered {days} days ago, outside the "
                           f"{RETURN_WINDOW_DAYS}-day returns window."}
    return_id = backends.next_id("RET")
    backends.RETURNS[return_id] = {"order_id": order["order_id"], "reason": reason,
                                   "state": "open", "customer_id": ctx.customer_id}
    return {"return_id": return_id, "order_id": order["order_id"], "state": "open",
            "instructions": "A prepaid label has been emailed to the address on the account."}


@tool(risk="critical", writes=True, agent="returns",
      description=("Issue a refund against an order. Refunds at or above the approval "
                   "threshold require a verified human approval token."))
def refund_order(ctx: RequestContext, order_id: str, amount: float, reason: str,
                 approval_token: str = "") -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err

    if amount <= 0:
        return {"error": "invalid_amount", "message": "A refund must be a positive amount."}

    # The refundable ceiling is the order total minus whatever has already been
    # refunded. Without the second term, three €89.99 refunds against an
    # €89.99 order each pass the "not more than the total" check individually.
    already = order.get("refunded", 0.0)
    remaining = round(order["total"] - already, 2)
    if amount > remaining:
        return {"error": "exceeds_order_value",
                "message": f"The most that can still be refunded on {order['order_id']} is "
                           f"{order['currency']} {remaining:.2f}."}

    params = {"order_id": order["order_id"], "amount": round(float(amount), 2)}
    needs_approval = amount >= config.REFUND_APPROVAL_THRESHOLD

    if needs_approval:
        if not approval_token:
            return approvals.request_approval(
                ctx, action="refund_order", params=params,
                reason=(f"Refund of {order['currency']} {amount:.2f} on {order['order_id']} "
                        f"is at or above the {order['currency']} "
                        f"{config.REFUND_APPROVAL_THRESHOLD:.2f} approval threshold."))

        result = approvals.redeem(approval_token, action="refund_order",
                                  params=params, requester=ctx.customer_id)
        if not result.ok:
            # Should never happen in normal operation. Alarmed because of that.
            audit.security_event(ctx, kind=f"approval_{result.reason}",
                                 detail=f"refund_order {params}")
            return {"error": "approval_invalid",
                    "message": "That approval could not be verified."}

    refund_id = backends.next_id("REF")
    order["refunded"] = round(already + amount, 2)
    backends.REFUND_LEDGER.append({
        "refund_id": refund_id, "order_id": order["order_id"], "amount": round(amount, 2),
        "currency": order["currency"], "reason": reason, "customer_id": ctx.customer_id,
        "approved": needs_approval,
    })
    return {"refund_id": refund_id, "order_id": order["order_id"],
            "amount": round(amount, 2), "currency": order["currency"],
            "approved_by_human": needs_approval}

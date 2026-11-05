"""Warranty claims and replacement dispatch."""
from __future__ import annotations

from core import config
from core.types import RequestContext
from security import approvals, audit
from tools import backends
from tools.registry import tool


def _owned_order(ctx: RequestContext, order_id: str):
    order = backends.get_order(order_id)
    if order is None or order["customer_id"] != ctx.customer_id:
        return None, {"error": "order_not_found",
                      "message": f"No order {order_id} on this account."}
    return order, None


@tool(risk="low", writes=True, agent="warranty",
      description="File a warranty claim for a faulty item on an order still under warranty.")
def file_warranty_claim(ctx: RequestContext, order_id: str, fault_description: str) -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    if not backends.warranty_active(order["order_id"]):
        w = backends.warranty_for(order["order_id"])
        detail = f"expired on {w['expires_at']}" if w else "no warranty is registered"
        return {"error": "warranty_inactive",
                "message": f"Order {order['order_id']}: {detail}."}
    if len(fault_description.strip()) < 10:
        return {"error": "insufficient_detail",
                "message": "Please describe the fault in a little more detail."}
    claim_id = backends.next_id("CLM")
    backends.CLAIMS[claim_id] = {"order_id": order["order_id"], "state": "open",
                                 "fault": fault_description[:500],
                                 "customer_id": ctx.customer_id}
    return {"claim_id": claim_id, "order_id": order["order_id"], "state": "open",
            "next_step": "An engineer will review the claim within two working days."}


@tool(risk="high", writes=True, agent="warranty",
      description=("Dispatch a free replacement unit. Replacements at or above the approval "
                   "threshold require a verified human approval token."))
def dispatch_replacement(ctx: RequestContext, order_id: str, reason: str,
                         approval_token: str = "") -> dict:
    order, err = _owned_order(ctx, order_id)
    if err:
        return err
    if not backends.warranty_active(order["order_id"]):
        return {"error": "warranty_inactive",
                "message": f"Order {order['order_id']} is not under active warranty."}
    if any(r["order_id"] == order["order_id"] for r in backends.REPLACEMENTS.values()):
        return {"error": "already_replaced",
                "message": f"A replacement has already been dispatched for "
                           f"{order['order_id']}."}

    value = round(float(order["total"]), 2)
    params = {"order_id": order["order_id"], "value": value}
    needs_approval = value >= config.REPLACEMENT_APPROVAL_THRESHOLD

    if needs_approval:
        if not approval_token:
            return approvals.request_approval(
                ctx, action="dispatch_replacement", params=params,
                reason=(f"Replacement worth {order['currency']} {value:.2f} is at or above "
                        f"the {order['currency']} "
                        f"{config.REPLACEMENT_APPROVAL_THRESHOLD:.2f} threshold."))
        result = approvals.redeem(approval_token, action="dispatch_replacement",
                                  params=params, requester=ctx.customer_id)
        if not result.ok:
            audit.security_event(ctx, kind=f"approval_{result.reason}",
                                 detail=f"dispatch_replacement {params}")
            return {"error": "approval_invalid",
                    "message": "That approval could not be verified."}

    rep_id = backends.next_id("REP")
    backends.REPLACEMENTS[rep_id] = {"order_id": order["order_id"], "reason": reason,
                                     "customer_id": ctx.customer_id}
    return {"replacement_id": rep_id, "order_id": order["order_id"],
            "value": value, "currency": order["currency"],
            "approved_by_human": needs_approval,
            "message": "A replacement will ship within three working days."}

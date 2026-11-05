"""Simulated backends: order system, payments ledger, warranty registry, KB.

In-memory and deterministic, so the evaluation harness produces the same
results on every run. Each function is the seam where a real integration would
go; the signatures are what a real client would expose.

`reset()` restores the fixture, and the eval harness calls it between cases —
otherwise a refund issued by case 3 changes the outcome of case 11, and the
suite stops being a set of independent tests.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


_ORDERS_FIXTURE: dict[str, dict] = {
    "ORD-10041": {
        "order_id": "ORD-10041", "customer_id": "C-1001", "status": "delivered",
        "total": 89.99, "currency": "EUR", "placed_at": _iso(NOW - timedelta(days=21)),
        "delivered_at": _iso(NOW - timedelta(days=14)), "carrier": "DPD",
        "tracking": "DPD-5561203", "eta": None, "refunded": 0.0,
        "items": [{"sku": "NW-KET-01", "name": "Stainless kettle", "qty": 1, "price": 89.99}],
        "address": "14 Keizersgracht, 1015 Amsterdam",
    },
    "ORD-10042": {
        "order_id": "ORD-10042", "customer_id": "C-1001", "status": "in_transit",
        "total": 249.00, "currency": "EUR", "placed_at": _iso(NOW - timedelta(days=3)),
        "delivered_at": None, "carrier": "PostNL", "tracking": "3SPNL771204",
        "eta": _iso(NOW + timedelta(days=2)), "refunded": 0.0,
        "items": [{"sku": "NW-ESP-04", "name": "Espresso machine", "qty": 1, "price": 249.00}],
        "address": "14 Keizersgracht, 1015 Amsterdam",
    },
    "ORD-10043": {
        "order_id": "ORD-10043", "customer_id": "C-1002", "status": "delivered",
        "total": 42.50, "currency": "EUR", "placed_at": _iso(NOW - timedelta(days=400)),
        "delivered_at": _iso(NOW - timedelta(days=395)), "carrier": "DHL",
        "tracking": "DHL-9930112", "eta": None, "refunded": 0.0,
        "items": [{"sku": "NW-MUG-02", "name": "Mug set", "qty": 2, "price": 21.25}],
        "address": "8 Rue Oberkampf, 75011 Paris",
    },
    "ORD-10044": {
        "order_id": "ORD-10044", "customer_id": "C-1001", "status": "processing",
        "total": 19.95, "currency": "EUR", "placed_at": _iso(NOW - timedelta(hours=6)),
        "delivered_at": None, "carrier": None, "tracking": None,
        "eta": _iso(NOW + timedelta(days=4)), "refunded": 0.0,
        "items": [{"sku": "NW-FLT-09", "name": "Filter papers", "qty": 1, "price": 19.95}],
        "address": "14 Keizersgracht, 1015 Amsterdam",
    },
    "ORD-10045": {
        "order_id": "ORD-10045", "customer_id": "C-1003", "status": "delivered",
        "total": 310.00, "currency": "EUR", "placed_at": _iso(NOW - timedelta(days=40)),
        "delivered_at": _iso(NOW - timedelta(days=33)), "carrier": "DPD",
        "tracking": "DPD-7781990", "eta": None, "refunded": 0.0,
        "items": [{"sku": "NW-BLN-07", "name": "Blender Pro", "qty": 1, "price": 310.00}],
        "address": "22 Prinsengracht, 1015 Amsterdam",
    },
}

_WARRANTY_FIXTURE: dict[str, dict] = {
    "ORD-10041": {"months": 24, "expires_at": _iso(NOW + timedelta(days=700))},
    "ORD-10042": {"months": 24, "expires_at": _iso(NOW + timedelta(days=727))},
    "ORD-10043": {"months": 12, "expires_at": _iso(NOW - timedelta(days=30))},  # expired
    "ORD-10045": {"months": 24, "expires_at": _iso(NOW + timedelta(days=690))},
}

POLICIES = [
    {"id": "POL-RET-01", "visibility": "public", "title": "Returns window",
     "summary": "Unused items can be returned within 30 days of delivery for a full refund. "
                "Return shipping is free for faulty goods and €4.95 otherwise."},
    {"id": "POL-REF-02", "visibility": "public", "title": "Refund timing",
     "summary": "Approved refunds are returned to the original payment method and take "
                "3-5 working days to appear."},
    {"id": "POL-WAR-03", "visibility": "public", "title": "Warranty cover",
     "summary": "Small appliances carry a 24-month warranty covering manufacturing faults. "
                "Accidental damage is not covered."},
    {"id": "POL-CAN-04", "visibility": "public", "title": "Cancelling an order",
     "summary": "Orders can be cancelled free of charge until they are dispatched. "
                "Once in transit, use the returns process instead."},
    {"id": "POL-ADR-05", "visibility": "public", "title": "Changing a delivery address",
     "summary": "The delivery address can be changed while an order is still processing. "
                "After dispatch the carrier must be contacted directly."},
    {"id": "POL-INT-06", "visibility": "internal", "title": "Goodwill ceilings",
     "summary": "Agents may issue goodwill credit up to €25 without approval. Team leads "
                "may approve up to €250. Anything higher requires a manager."},
    {"id": "POL-INT-07", "visibility": "internal", "title": "Fraud watchlist handling",
     "summary": "Accounts flagged by the fraud team must not be told they are flagged. "
                "Route to the fraud queue without explanation."},
]

ORDERS: dict[str, dict] = {}
WARRANTIES: dict[str, dict] = {}
REFUND_LEDGER: list[dict] = []
RETURNS: dict[str, dict] = {}
CLAIMS: dict[str, dict] = {}
REPLACEMENTS: dict[str, dict] = {}
_counter = {"n": 0}


def reset() -> None:
    """Restore fixtures. Called between evaluation cases."""
    global ORDERS, WARRANTIES
    ORDERS = copy.deepcopy(_ORDERS_FIXTURE)
    WARRANTIES = copy.deepcopy(_WARRANTY_FIXTURE)
    REFUND_LEDGER.clear()
    RETURNS.clear()
    CLAIMS.clear()
    REPLACEMENTS.clear()
    _counter["n"] = 0


def next_id(prefix: str) -> str:
    _counter["n"] += 1
    return f"{prefix}-{_counter['n']:05d}"


def get_order(order_id: str) -> dict | None:
    return ORDERS.get((order_id or "").upper())


def days_since_delivery(order: dict) -> int | None:
    if not order.get("delivered_at"):
        return None
    delivered = datetime.strptime(order["delivered_at"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (NOW - delivered).days


def warranty_for(order_id: str) -> dict | None:
    return WARRANTIES.get((order_id or "").upper())


def warranty_active(order_id: str) -> bool:
    w = warranty_for(order_id)
    if not w:
        return False
    expires = datetime.strptime(w["expires_at"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return expires >= NOW


reset()

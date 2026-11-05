"""Append-only audit log.

Separate from the observability trace on purpose. Traces are for debugging and
are sampled and expired aggressively; the audit log answers "who did what to
whose money" and is retained for years. Mixing the two means either paying
trace prices for audit retention, or sampling away the record of a refund.

Arguments are redacted before they are written: an audit log that captures a
card number an attacker pasted has turned a transient leak into a durable one.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from core import config

_lock = threading.Lock()

_PAN = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SECRETISH = re.compile(r"\b(sk-[A-Za-z0-9_\-]{10,}|gsk_[A-Za-z0-9_\-]{10,}|AKIA[0-9A-Z]{12,})")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        out = _SECRETISH.sub("[REDACTED_SECRET]", value)
        out = _PAN.sub("[REDACTED_PAN]", out)
        out = _EMAIL.sub(lambda m: m.group(0)[0] + "***@" + m.group(0).split("@")[1], out)
        return out[:500]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value[:20]]
    return value


def record(ctx, *, tool: str, arguments: dict, outcome: str, risk: str,
           detail: str | None = None, writes: bool = False,
           duration_ms: int = 0) -> None:
    entry = {
        "ts": time.time(),
        "request_id": getattr(ctx, "request_id", ""),
        "conversation_id": getattr(ctx, "conversation_id", ""),
        "customer_id": getattr(ctx, "customer_id", ""),
        "tool": tool,
        "arguments": redact(arguments),
        "outcome": outcome,
        "risk": risk,
        "writes": writes,
        "detail": redact(detail) if detail else None,
        "duration_ms": duration_ms,
    }
    with _lock:
        with open(config.AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")


def security_event(ctx, *, kind: str, detail: str, severity: str = "P1") -> None:
    """A failure that should be impossible in normal operation.

    Approval-token verification failures land here. They are alarmed at P1 not
    because they are frequent but because they are not: a non-zero rate means
    either a bug in the approval flow or somebody trying to forge one.
    """
    entry = {
        "ts": time.time(),
        "request_id": getattr(ctx, "request_id", ""),
        "conversation_id": getattr(ctx, "conversation_id", ""),
        "customer_id": getattr(ctx, "customer_id", ""),
        "event": "security",
        "kind": kind,
        "severity": severity,
        "detail": redact(detail),
    }
    with _lock:
        with open(config.AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")


def read_all() -> list[dict]:
    try:
        with open(config.AUDIT_LOG, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def clear() -> None:
    """Test-suite helper only. Never called by the running system."""
    open(config.AUDIT_LOG, "w").close()

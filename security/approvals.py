"""Human approval for irreversible actions.

THE CENTRAL CLAIM OF THIS MODULE
--------------------------------
Approval is verified by the tool at the moment of execution, against a signed
record created out of band. It is never asserted in the conversation.

If the agent could *remember* that a human approved something, a crafted
message could manufacture that memory. "My manager already approved this
refund" contains no injection markers and reads as an ordinary sentence, so
pattern matching will not reliably catch it — and it does not need to, because
the claim is simply irrelevant: `redeem()` reads a signed record, not a
transcript.

A token is bound to the exact parameters of the action. A token approved for
a €40 refund cannot be replayed against a €4,000 one, because the amount is
inside the signature.

Tokens are single-use. A multi-day approval window plus a reusable token is a
standing authorisation nobody intended to issue.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core import config


# --------------------------------------------------------------------------
# pending request store (durable)
# --------------------------------------------------------------------------
# A pending approval must NOT live in conversation state. An approver may act
# tomorrow; a session expiry or a redeploy would silently lose the request and
# the customer would be waiting on something that no longer exists.
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.SESSION_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS approval_requests (
        request_id TEXT PRIMARY KEY, conversation_id TEXT, customer_id TEXT,
        action TEXT, params TEXT, reason TEXT, state TEXT, created_at REAL,
        decided_at REAL, approver TEXT, jti TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS redeemed_tokens (
        jti TEXT PRIMARY KEY, redeemed_at REAL)""")
    conn.commit()
    return conn


@dataclass
class VerificationResult:
    ok: bool
    reason: str = ""
    claims: dict[str, Any] = field(default_factory=dict)


def _params_fingerprint(params: dict) -> str:
    """Order-independent, type-stable fingerprint of the action parameters."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(config.APPROVAL_SIGNING_KEY.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def _unsign(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(config.APPROVAL_SIGNING_KEY.encode(), body.encode(),
                        hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).decode().rstrip("=")
    # Constant-time compare: a timing side channel on a signature check is a
    # real, if slow, forgery oracle.
    if not hmac.compare_digest(sig, expected_b64):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:
        return None


# --------------------------------------------------------------------------
# request / approve / redeem
# --------------------------------------------------------------------------
def request_approval(ctx, action: str, params: dict, reason: str) -> dict:
    """Create a durable approval request and return the agent-visible result."""
    request_id = f"APR-{uuid.uuid4().hex[:8].upper()}"
    conn = _db()
    conn.execute(
        "INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (request_id, ctx.conversation_id, ctx.customer_id, action,
         json.dumps(params, sort_keys=True, default=str), reason, "pending",
         time.time(), None, None, None))
    conn.commit()
    conn.close()
    return {
        "approval_required": True,
        "approval_request_id": request_id,
        "action": action,
        "message": reason,
    }


def approve(request_id: str, approver: str) -> dict:
    """Called by the approval console (`python run.py approvals approve ...`).

    In production this is a separate authenticated service — ServiceNow, an
    internal console, a Slack action. It is deliberately NOT reachable from the
    agent: no tool calls this function.
    """
    conn = _db()
    row = conn.execute(
        "SELECT action, params, state, customer_id FROM approval_requests WHERE request_id=?",
        (request_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "not_found", "message": f"no approval request {request_id}"}
    action, params_json, state, customer_id = row
    if state != "pending":
        conn.close()
        return {"error": "not_pending", "message": f"request is already {state}"}

    # Segregation of duties: the requester cannot be the approver.
    if approver == customer_id:
        conn.close()
        return {"error": "self_approval", "message": "approver and requester must differ"}

    jti = uuid.uuid4().hex
    params = json.loads(params_json)
    token = _sign({
        "jti": jti,
        "action": action,
        "params_fp": _params_fingerprint(params),
        "approver": approver,
        "requester": customer_id,
        "exp": time.time() + config.APPROVAL_TTL_SECONDS,
        "request_id": request_id,
    })
    conn.execute("UPDATE approval_requests SET state=?, decided_at=?, approver=?, jti=? "
                 "WHERE request_id=?", ("approved", time.time(), approver, jti, request_id))
    conn.commit()
    conn.close()
    return {"ok": True, "request_id": request_id, "approval_token": token,
            "expires_in_seconds": config.APPROVAL_TTL_SECONDS}


def reject(request_id: str, approver: str, note: str = "") -> dict:
    conn = _db()
    conn.execute("UPDATE approval_requests SET state=?, decided_at=?, approver=? "
                 "WHERE request_id=? AND state='pending'",
                 ("rejected", time.time(), approver, request_id))
    changed = conn.total_changes
    conn.commit()
    conn.close()
    if not changed:
        return {"error": "not_pending", "message": "no pending request with that id"}
    return {"ok": True, "request_id": request_id, "state": "rejected", "note": note}


def pending(conversation_id: str | None = None) -> list[dict]:
    conn = _db()
    sql = ("SELECT request_id, conversation_id, customer_id, action, params, reason, state "
           "FROM approval_requests WHERE state='pending'")
    args: tuple = ()
    if conversation_id:
        sql += " AND conversation_id=?"
        args = (conversation_id,)
    rows = conn.execute(sql + " ORDER BY created_at", args).fetchall()
    conn.close()
    return [{"request_id": r[0], "conversation_id": r[1], "customer_id": r[2],
             "action": r[3], "params": json.loads(r[4]), "reason": r[5], "state": r[6]}
            for r in rows]


def redeem(token: str, action: str, params: dict, requester: str) -> VerificationResult:
    """Verify and consume an approval token. THIS is the control.

    Every failure path here is a security event: in normal operation none of
    them should ever occur, which is exactly why they are alarmed rather than
    quietly returned. A control you are certain cannot fail is a control
    nobody checks.
    """
    claims = _unsign(token or "")
    if claims is None:
        return VerificationResult(False, "invalid_signature")
    if claims.get("action") != action:
        return VerificationResult(False, "action_mismatch", claims)
    if claims.get("params_fp") != _params_fingerprint(params):
        # The token was approved for a different set of parameters. This is the
        # replay that parameter binding exists to stop.
        return VerificationResult(False, "parameter_mismatch", claims)
    if float(claims.get("exp", 0)) < time.time():
        return VerificationResult(False, "expired", claims)
    if claims.get("requester") != requester:
        return VerificationResult(False, "requester_mismatch", claims)
    if claims.get("approver") == claims.get("requester"):
        return VerificationResult(False, "self_approval", claims)

    conn = _db()
    already = conn.execute("SELECT 1 FROM redeemed_tokens WHERE jti=?",
                           (claims["jti"],)).fetchone()
    if already:
        conn.close()
        return VerificationResult(False, "already_redeemed", claims)
    conn.execute("INSERT INTO redeemed_tokens VALUES (?,?)", (claims["jti"], time.time()))
    conn.execute("UPDATE approval_requests SET state='redeemed' WHERE request_id=?",
                 (claims.get("request_id"),))
    conn.commit()
    conn.close()
    return VerificationResult(True, "ok", claims)

"""Input and output guardrails.

Deterministic patterns only. No model call. A guardrail that costs as much as
the request it guards gets switched off within two quarters, and a guardrail
that hallucinates is worse than none.

Scope note, and it matters: `check_input` examines ONLY the newest user
message. With session history in play, scanning the accumulated conversation
means a message blocked on turn 1 re-trips on every later turn — the
conversation becomes permanently stuck, and the reported finding points at the
wrong message, so the symptom actively misleads whoever investigates.

What these do and do not buy you
--------------------------------
Input and output guardrails reduce the PROBABILITY of a bad action. They do
not bound the CONSEQUENCE. That is the tool layer's job (see approvals.py).
Prompt injection is not solved here and claiming otherwise would be dishonest:
the reason a crafted message cannot move money is that `refund_order` checks
a signed approval record, not that a regex caught the phrasing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core import config


@dataclass
class GuardrailResult:
    allowed: bool
    category: str = ""
    message: str = ""


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------
INJECTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+|your\s+|the\s+)?(previous|prior|above|earlier)"
    r"\s+(instruction|prompt|rule|direction)|"
    r"you\s+are\s+now\s+(a|an|the)\b|"
    r"(reveal|print|show|repeat)\s+(me\s+)?(your|the)\s+(system\s+)?(prompt|instructions)|"
    r"\bdeveloper\s+mode\b|\bjailbreak\b",
    re.I)

AUTHORITY_CLAIM = re.compile(
    r"\b(my|the)\s+(manager|supervisor|team lead|lawyer|account manager)\s+"
    r"(has\s+)?(already\s+)?(approved|authorised|authorized|signed off|said yes)\b|"
    r"\b(support|security|the)\s+(team|agent|desk)\s+(already\s+)?(approved|authorised|authorized|said)\b|"
    r"\byou\s+(already\s+)?(approved|authorised|authorized|agreed to)\b|"
    r"\bthis\s+(is|was)\s+(pre[- ]?)?approved\b",
    re.I)

SECRET = re.compile(r"\b(sk-[A-Za-z0-9_\-]{10,}|gsk_[A-Za-z0-9_\-]{10,}|AKIA[0-9A-Z]{12,}|"
                    r"-----BEGIN (RSA )?PRIVATE KEY-----)")

PAN_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def looks_like_card(text: str) -> bool:
    """Luhn-validate before flagging.

    A bare 13-19 digit regex flags order numbers, tracking numbers and IBANs.
    A guardrail is only credible if you test what it lets through, not just
    what it blocks — so the check is Luhn, not length.
    """
    for match in PAN_CANDIDATE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def check_input(text: str) -> GuardrailResult:
    """Scan ONLY the newest user message."""
    text = text or ""
    if len(text) > config.MAX_INPUT_CHARS:
        return GuardrailResult(False, "oversized",
                               "That message is too long for me to process. Could you "
                               "summarise the problem in a few sentences?")
    if SECRET.search(text):
        return GuardrailResult(False, "pasted_secret",
                               "That message appears to contain a credential. I haven't "
                               "stored it — please rotate it and don't share it again.")
    if looks_like_card(text):
        return GuardrailResult(False, "payment_data",
                               "Please don't share full card numbers here. I never need "
                               "them, and refunds go back to the original payment method "
                               "automatically.")
    if INJECTION.search(text):
        return GuardrailResult(False, "prompt_injection",
                               "I can only help with orders, returns, warranties and "
                               "policy questions on this account.")
    if AUTHORITY_CLAIM.search(text):
        # Detected because it is common. Detection is NOT the control: the
        # control is that approvals are verified against a signed record, so
        # the claim is irrelevant either way.
        return GuardrailResult(False, "authority_claim",
                               "I'm not able to act on an approval described in chat. "
                               "Approvals are confirmed through our internal review, and "
                               "I'll raise one automatically if this needs it.")
    if not text.strip():
        return GuardrailResult(False, "empty", "Could you tell me what you need help with?")
    return GuardrailResult(True)


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------
INTERNAL_POLICY_IDS = re.compile(r"\bPOL-INT-\d+\b")
UNQUALIFIED_PROMISE = re.compile(
    r"\b(guarantee|guaranteed|i promise|definitely will|will certainly|"
    r"you will receive it (tomorrow|today))\b", re.I)
OTHER_CUSTOMER = re.compile(r"\bC-\d{4}\b")

SAFE_NOT_FOUND = ("I can't see an order with that number on this account. If it was placed "
                  "with a different email address, sign in with that one and I can help.")


def check_output(text: str, ctx=None) -> GuardrailResult:
    text = text or ""
    if SECRET.search(text) or looks_like_card(text):
        return GuardrailResult(False, "credential_echo",
                               "I've hidden that detail for safety. Could you rephrase "
                               "what you need?")
    if INTERNAL_POLICY_IDS.search(text):
        return GuardrailResult(False, "internal_policy_leak",
                               "I can only share our published policies. Let me know what "
                               "you'd like to check and I'll quote the customer policy.")
    if UNQUALIFIED_PROMISE.search(text):
        return GuardrailResult(False, "unqualified_commitment",
                               "I don't want to promise a date I can't control — I'll give "
                               "you the current status instead.")
    if ctx is not None:
        for match in OTHER_CUSTOMER.finditer(text):
            if match.group(0) != getattr(ctx, "customer_id", None):
                return GuardrailResult(False, "cross_customer_reference",
                                       "I can only discuss this account.")
    return GuardrailResult(True)

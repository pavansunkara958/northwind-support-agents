"""Model access, normalised across three providers plus a deterministic stub.

The rest of the system calls `complete()` and gets back an `LLMResponse`.
Nothing above this module knows which provider is in use.

OFFLINE MODE IS A STUB, NOT A MODEL. It is a small rule table that produces
tool calls and final messages deterministically. It exists so that:

  * a reviewer can clone and run the system with no API key, and
  * the security / tool-layer evaluation is reproducible byte for byte.

It must never be presented as evidence about model quality. The evaluation
harness separates the two explicitly: Suite A (deterministic, offline) tests
the system's own code paths; Suite B (live) tests routing and answer quality
and requires a real key.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from . import config
from .types import LLMResponse, ToolCall

_client = None


# --------------------------------------------------------------------------
# provider clients
# --------------------------------------------------------------------------
def _strip_authorization(request) -> None:
    """Some gateways forward Authorization verbatim to the upstream provider.

    When the gateway authenticates with its own header, an Authorization header
    we never meant to send upstream produces a confusing 401 that appears to
    come from us. Removing it on the way out is the fix.
    """
    request.headers.pop("authorization", None)


def _build_client():
    from openai import OpenAI

    if config.PROVIDER == "gateway":
        if config.GATEWAY_HEADER_NAME.lower() == "authorization":
            return OpenAI(base_url=config.GATEWAY_BASE_URL, api_key=config.GATEWAY_KEY)
        import httpx

        http_client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=15.0),
            event_hooks={"request": [_strip_authorization]},
        )
        return OpenAI(
            base_url=config.GATEWAY_BASE_URL,
            api_key="unused",  # stripped before it reaches the wire
            default_headers={config.GATEWAY_HEADER_NAME: config.GATEWAY_KEY},
            http_client=http_client,
        )
    return OpenAI(api_key=config.OPENAI_API_KEY)


def client():
    global _client
    if _client is None:
        _client = _build_client()
    return _client


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------
def complete(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    model: str | None = None,
    json_mode: bool = False,
    agent: str = "",
) -> LLMResponse:
    if config.PROVIDER == "offline":
        return _offline_complete(messages, tools, json_mode=json_mode, agent=agent)

    model = model or config.MODEL_SPECIALIST
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    resp = client().chat.completions.create(**kwargs)
    choice = resp.choices[0].message
    calls: list[ToolCall] = []
    for tc in getattr(choice, "tool_calls", None) or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {"__malformed__": tc.function.arguments}
        calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

    usage = getattr(resp, "usage", None)
    return LLMResponse(
        content=choice.content,
        tool_calls=calls,
        usage={
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        },
        model=model,
    )


# --------------------------------------------------------------------------
# deterministic offline stub
# --------------------------------------------------------------------------
ORDER_RE = re.compile(r"\bORD-\d{4,}\b", re.I)
AMOUNT_RE = re.compile(r"(?:[$€£]\s?|\bfor\s+|\bof\s+)(\d{1,6}(?:\.\d{1,2})?)\b")

# A policy question with no order number goes to Knowledge regardless of the
# nouns in it: "how long do I have to return something" is about the returns
# POLICY, not about returning a specific item.
_POLICY_Q = re.compile(
    r"\b(policy|policies|how long|how do i|how does|how much|what is|what's your|"
    r"am i able|eligible|terms|do you (accept|offer|cover))\b", re.I)

_ROUTES = [
    ("returns", r"\b(refund|return|returning|money back|send it back|rma|restock)\b"),
    ("warranty", r"\b(warrant\w*|broken|faulty|defect\w*|stopped working|replacement|died)\b"),
    ("order", r"\b(order|status|track\w*|deliver\w*|shipment|shipping|parcel|cancel|"
              r"address|where is|where's)\b"),
]

# agent -> list of (regex, tool name, argument builder)
_TOOL_RULES: dict[str, list[tuple[str, str, Any]]] = {
    "order": [
        (r"\bcancel\b", "cancel_order", lambda t: {"order_id": _order(t)}),
        (r"\baddress\b|\bdeliver(y)? to\b", "change_shipping_address",
         lambda t: {"order_id": _order(t), "new_address": _tail_address(t)}),
        (r".*", "get_order_status", lambda t: {"order_id": _order(t)}),
    ],
    "returns": [
        (r"\brefund\b|\bmoney back\b", "refund_order",
         lambda t: {"order_id": _order(t), "amount": _amount(t), "reason": "customer request"}),
        (r".*", "start_return", lambda t: {"order_id": _order(t), "reason": "customer request"}),
    ],
    "warranty": [
        (r"\breplacement\b|\bsend (me )?a new\b", "dispatch_replacement",
         lambda t: {"order_id": _order(t), "reason": "warranty replacement"}),
        (r".*", "file_warranty_claim",
         lambda t: {"order_id": _order(t), "fault_description": t[:200]}),
    ],
    "knowledge": [
        (r".*", "search_policies", lambda t: {"query": t[:120]}),
    ],
}


def _order(text: str) -> str:
    m = ORDER_RE.search(text or "")
    return m.group(0).upper() if m else ""


def _amount(text: str) -> float:
    """Extract a money amount.

    Order identifiers are stripped first. Without that, "refund ORD-10041"
    yields an amount of 10041 — a four-figure refund request produced by a
    regex, which the tool layer then has to catch. It does catch it, but a
    control firing because of a parsing bug is luck, not design.
    """
    cleaned = ORDER_RE.sub(" ", text or "")
    m = AMOUNT_RE.search(cleaned)
    return float(m.group(1)) if m else 0.0


def _tail_address(text: str) -> str:
    m = re.search(r"(?:to|address(?: is)?)[: ]+(.{5,120})$", text or "", re.I)
    return (m.group(1) if m else "unspecified").strip()


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


def _offline_complete(messages, tools, json_mode: bool, agent: str) -> LLMResponse:
    text = _last_user(messages)
    usage = {"input_tokens": sum(len(str(m.get("content") or "")) // 4 for m in messages),
             "output_tokens": 24}

    # Router: return a JSON classification.
    if json_mode or agent == "orchestrator":
        dest = "knowledge"
        if not (_POLICY_Q.search(text) and not ORDER_RE.search(text)):
            for name, pattern in _ROUTES:
                if re.search(pattern, text, re.I):
                    dest = name
                    break
        payload = {"destination": dest, "confidence": 0.9,
                   "reason": "offline stub: keyword match"}
        return LLMResponse(content=json.dumps(payload), usage=usage)

    # After a tool result, compose a reply from it rather than calling again.
    if messages and messages[-1].get("role") == "tool":
        return LLMResponse(content=_summarise_tool_result(messages[-1].get("content", "")),
                           usage=usage)

    for pattern, tool_name, build in _TOOL_RULES.get(agent, []):
        if re.search(pattern, text, re.I):
            available = {t["function"]["name"] for t in (tools or [])}
            if tool_name not in available:
                continue
            return LLMResponse(
                tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}",
                                     name=tool_name, arguments=build(text))],
                usage=usage,
            )

    return LLMResponse(content="I need a little more detail to help with that.", usage=usage)


def _summarise_tool_result(raw: str) -> str:
    """Turn a tool's JSON result into a sentence, deterministically."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)[:400]

    if not isinstance(data, dict):
        return str(data)[:400]

    if data.get("error"):
        return f"I couldn't complete that: {data.get('message') or data['error']}"
    if data.get("approval_required"):
        return (f"That needs a human approval before I can action it. "
                f"I've raised request {data.get('approval_request_id')} — "
                f"{data.get('message', '')}").strip()
    if data.get("status") and data.get("order_id"):
        bits = [f"Order {data['order_id']} is {data['status']}"]
        if data.get("carrier"):
            bits.append(f"with {data['carrier']} (tracking {data.get('tracking', 'pending')})")
        if data.get("eta"):
            bits.append(f"ETA {data['eta']}")
        return ", ".join(bits) + "."
    if data.get("refund_id"):
        return (f"Refund {data['refund_id']} of "
                f"{data.get('currency', 'EUR')} {data.get('amount')} has been issued for "
                f"order {data.get('order_id')}.")
    if data.get("return_id"):
        return (f"Return {data['return_id']} is open for order {data.get('order_id')}. "
                f"{data.get('instructions', '')}").strip()
    if data.get("claim_id"):
        return f"Warranty claim {data['claim_id']} has been filed for order {data.get('order_id')}."
    if data.get("results") is not None:
        results = data["results"]
        if not results:
            return "I couldn't find a policy that covers that."
        return results[0].get("summary", "See policy.")
    return json.dumps(data)[:400]

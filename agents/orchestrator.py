"""Supervisor: guardrail, classify, delegate, persist.

The orchestrator holds NO TOOLS. That is a design decision, not an oversight:
an agent with no tools cannot half-answer a request and hand over a partially
actioned mess. Its entire job is to decide who owns the turn.

Order of operations is itself a control:

    input guardrail  ->  route  ->  specialist  ->  output guardrail  ->  persist

The input guardrail runs before any model call, so a blocked message costs
nothing and reaches nothing. Persistence happens last, so a blocked turn does
not pollute the working memory with structure extracted from a hostile
message.
"""
from __future__ import annotations

import json
import time

from agents.specialists import SPECIALISTS
from core import config, llm
from core.types import AgentResult, RequestContext
from memory import session, working
from ops import observability
from security import guardrails

ROUTER_SYSTEM = (
    "You route customer support messages for an online retailer to one team. "
    "Reply with JSON only: {\"destination\": <team>, \"confidence\": <0-1>, "
    "\"reason\": <short>}.\n\n"
    "Teams:\n"
    "- order: order status, tracking, delivery, cancelling before dispatch, address changes\n"
    "- returns: returning an item, refunds, money back\n"
    "- warranty: faulty or broken items, warranty cover, replacements\n"
    "- knowledge: policy and how-to questions with no specific order action\n\n"
    "Choose the team that owns the OUTCOME the customer wants. A faulty item the "
    "customer wants money back for is returns; a faulty item they want fixed or "
    "replaced is warranty. If a message spans two teams, pick the primary one."
)

VALID_ROUTES = set(SPECIALISTS)
FALLBACK_ROUTE = "knowledge"


def route(text: str) -> tuple[str, str, float]:
    """Classify a message. Returns (destination, reason, confidence)."""
    messages = [{"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user", "content": text}]
    response = llm.complete(messages, json_mode=True, model=config.MODEL_ROUTER,
                            agent="orchestrator")
    try:
        data = json.loads(response.content or "{}")
    except json.JSONDecodeError:
        return FALLBACK_ROUTE, "router returned unparseable output", 0.0

    dest = str(data.get("destination", "")).strip().lower()
    if dest not in VALID_ROUTES:
        # A hallucinated destination falls back rather than failing the turn.
        # The fallback agent is read-only, so a bad route cannot cause a bad
        # action — it can only produce an unhelpful answer.
        return FALLBACK_ROUTE, f"unknown destination {dest!r}", 0.0
    return dest, str(data.get("reason", ""))[:200], float(data.get("confidence", 0) or 0)


def handle(ctx: RequestContext, user_text: str) -> AgentResult:
    started = time.time()
    trace_id = observability.new_trace_id()

    with observability.span("support.request", trace_id=trace_id, kind="server",
                            **{"customer.id": ctx.customer_id,
                               "conversation.id": ctx.conversation_id,
                               "request.id": ctx.request_id}):

        # 1 — input guardrail, on the NEWEST message only.
        verdict = guardrails.check_input(user_text)
        with observability.span("guardrail.input",
                                **{"guardrail.allowed": verdict.allowed,
                                   "guardrail.category": verdict.category}):
            pass
        if not verdict.allowed:
            session.append(ctx.conversation_id, "user", user_text)
            session.append(ctx.conversation_id, "assistant", verdict.message,
                           agent="guardrail")
            return AgentResult(agent="guardrail", reply=verdict.message, blocked=True,
                               block_reason=f"input:{verdict.category}",
                               latency_ms=int((time.time() - started) * 1000),
                               request_id=ctx.request_id)

        # 2 — route.
        with observability.span("orchestrator.route") as sp:
            destination, reason, confidence = route(user_text)
            sp["attributes"].update({"route.destination": destination,
                                     "route.confidence": confidence})

        # 3 — load memory and delegate.
        wm = working.load(ctx.conversation_id, ctx.customer_id)
        wm.observe(user_text)
        if not wm.current_order and wm.orders:
            pass  # observe() already ordered them
        wm.note_intent(destination)

        history = session.history(ctx.conversation_id)
        agent = SPECIALISTS[destination]
        result = agent.run(ctx, user_text, history, wm)
        result.route = destination
        result.route_reason = reason

        # 4 — persist last, so a blocked turn leaves no residue.
        session.append(ctx.conversation_id, "user", user_text)
        session.append(ctx.conversation_id, "assistant", result.reply, agent=destination)
        working.save(wm)

        result.latency_ms = int((time.time() - started) * 1000)
        result.request_id = ctx.request_id
        return result

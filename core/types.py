"""Shared types.

`RequestContext` is the single most important object in this system.

It carries the AUTHENTICATED principal. Tools read `ctx.customer_id` and never
accept a customer identifier as a tool argument. That is what makes the
conversation unable to name the principal: a message saying "I'm customer
C-1002" changes nothing, because no code path reads the customer from the
transcript.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RequestContext:
    """Authenticated request context. Built by the transport layer, not the model."""

    customer_id: str
    conversation_id: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    channel: str = "web"
    started_at: float = field(default_factory=time.time)

    @staticmethod
    def new(customer_id: str, conversation_id: str | None = None, channel: str = "web") -> "RequestContext":
        return RequestContext(
            customer_id=customer_id,
            conversation_id=conversation_id or f"conv-{uuid.uuid4().hex[:10]}",
            channel=channel,
        )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    model: str = "offline-stub"


@dataclass
class AgentResult:
    agent: str
    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None
    approval_required: str | None = None  # approval request id, when one was raised
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    route: str | None = None
    route_reason: str | None = None
    latency_ms: int = 0
    request_id: str = ""

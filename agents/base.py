"""Specialist agent: a system prompt, a tool allow-list, and a bounded loop.

The allow-list is the security boundary, not the prompt. The Knowledge agent
cannot issue a refund because `refund_order` is not in its tool list — no
phrasing, no injected instruction and no model error can reach a function that
was never offered. Instructions shape behaviour; tool scope defines
capability, and only the second is a control.

That distinction is what makes a misroute a quality problem rather than an
incident. If a warranty question lands on the Order agent, the worst outcome
is an unhelpful answer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from core import config, llm
from core.types import AgentResult, RequestContext
from memory import working
from ops import observability
from security import guardrails
from tools import registry


@dataclass
class Agent:
    name: str
    description: str
    instructions: str
    tool_names: list[str] = field(default_factory=list)

    def schemas(self) -> list[dict]:
        return registry.openai_schemas(self.tool_names) if self.tool_names else []

    def run(self, ctx: RequestContext, user_text: str, history: list[dict],
            wm: working.WorkingMemory) -> AgentResult:
        started = time.time()
        system = self.instructions
        context_block = wm.as_prompt_context()
        if context_block:
            system += "\n\nConversation context:\n" + context_block

        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        tool_calls_made: list[dict] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        approval_id: str | None = None
        reply = ""

        for _ in range(config.MAX_TOOL_ITERATIONS):
            with observability.span(f"agent.{self.name}.model", kind="client") as sp:
                response = llm.complete(messages, tools=self.schemas() or None,
                                        model=config.MODEL_SPECIALIST, agent=self.name)
                observability.model_span(sp, model=response.model, usage=response.usage,
                                         agent=self.name)
            usage["input_tokens"] += response.usage.get("input_tokens", 0)
            usage["output_tokens"] += response.usage.get("output_tokens", 0)

            if not response.tool_calls:
                reply = response.content or ""
                break

            messages.append({
                "role": "assistant", "content": response.content,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name,
                                             "arguments": registry.as_json(c.arguments)}}
                               for c in response.tool_calls]})

            for call in response.tool_calls:
                if call.name not in self.tool_names:
                    # The model asked for a tool this agent does not hold. Not
                    # an error worth failing the turn over, but it IS worth
                    # recording: a rising rate means the routing is wrong.
                    result = {"error": "tool_not_available",
                              "message": f"{self.name} cannot perform {call.name}"}
                else:
                    with observability.span(f"tool.{call.name}", kind="internal",
                                            **{"tool.name": call.name,
                                               "tool.risk": registry.REGISTRY[call.name].risk}):
                        result = registry.invoke(call.name, call.arguments, ctx)

                tool_calls_made.append({"name": call.name, "arguments": call.arguments,
                                        "result": result})
                if result.get("approval_required"):
                    approval_id = result.get("approval_request_id")
                    wm.pending_approval_id = approval_id
                if result.get("order_id"):
                    wm.touch_order(result["order_id"])

                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": registry.as_json(result)})
        else:
            reply = reply or ("I wasn't able to complete that in a reasonable number of "
                              "steps. Let me pass you to a colleague.")

        # Output guardrail runs on the final reply, last, so nothing downstream
        # can undo it.
        verdict = guardrails.check_output(reply, ctx)
        with observability.span("guardrail.output", **{"guardrail.allowed": verdict.allowed,
                                                       "guardrail.category": verdict.category}):
            pass
        if not verdict.allowed:
            return AgentResult(agent=self.name, reply=verdict.message, blocked=True,
                               block_reason=f"output:{verdict.category}",
                               tool_calls=tool_calls_made, usage=usage,
                               approval_required=approval_id,
                               latency_ms=int((time.time() - started) * 1000),
                               request_id=ctx.request_id)

        return AgentResult(agent=self.name, reply=reply, tool_calls=tool_calls_made,
                           usage=usage, approval_required=approval_id,
                           latency_ms=int((time.time() - started) * 1000),
                           request_id=ctx.request_id)

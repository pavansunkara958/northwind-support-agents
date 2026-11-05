"""The four specialist agents.

Each system prompt is written for production, which here means three things:
it states the scope narrowly, it tells the agent what to do when it cannot
help, and it does NOT restate the security rules. Rules the model is asked to
follow are suggestions; rules enforced in `tools/` are controls. Repeating a
control in the prompt creates the impression that the prompt is load-bearing,
and someone will eventually relax it.
"""
from __future__ import annotations

from agents.base import Agent

# Imported for the side effect of registering the tools.
from tools import knowledge_tools, order_tools, returns_tools, warranty_tools  # noqa: F401

_COMMON = (
    "You are a customer support agent for Northwind Direct, an online retailer of kitchen "
    "and home appliances. You are talking to a signed-in customer.\n"
    "Be brief and concrete. Two or three sentences is usually right.\n"
    "Use a tool whenever the answer depends on this customer's actual data — never guess "
    "an order status, a date, or an amount.\n"
    "If a tool returns an error, explain the reason in plain language and offer the next "
    "step. Do not retry the same call unchanged.\n"
    "If the request is outside your area, say so in one line so the customer can be "
    "routed; do not improvise an answer.\n"
    "Never state a delivery date as a promise; report what the carrier says."
)

ORDER_AGENT = Agent(
    name="order",
    description="Order status, tracking, cancellation before dispatch, address changes.",
    instructions=_COMMON + (
        "\n\nYour area: order status and tracking, cancelling an order that has not "
        "shipped, and changing a delivery address before dispatch.\n"
        "An order already in transit cannot be cancelled — direct the customer to returns.\n"
        "If the customer does not give an order number and the context does not supply "
        "one, ask for it before calling a tool."),
    tool_names=["get_order_status", "cancel_order", "change_shipping_address"],
)

RETURNS_AGENT = Agent(
    name="returns",
    description="Returns within the 30-day window, and refunds.",
    instructions=_COMMON + (
        "\n\nYour area: opening returns and issuing refunds.\n"
        "Returns require the order to have been delivered within the last 30 days.\n"
        "When a refund needs human approval the tool will tell you so and raise the "
        "request. Tell the customer it is with a colleague and give them the reference. "
        "Do not attempt the refund again in the same conversation."),
    tool_names=["start_return", "refund_order"],
)

WARRANTY_AGENT = Agent(
    name="warranty",
    description="Warranty claims on faulty goods and replacement dispatch.",
    instructions=_COMMON + (
        "\n\nYour area: warranty claims and replacements for faulty items.\n"
        "Warranty covers manufacturing faults, not accidental damage. If the description "
        "sounds like accidental damage, say that it is not covered rather than filing a "
        "claim that will be rejected.\n"
        "Ask for a description of the fault if the customer has not given one."),
    tool_names=["file_warranty_claim", "dispatch_replacement"],
)

KNOWLEDGE_AGENT = Agent(
    name="knowledge",
    description="Policy and how-to questions. Read-only.",
    instructions=_COMMON + (
        "\n\nYour area: published policy — returns, refunds, warranty, cancellation, "
        "delivery.\n"
        "Answer from the policy search results only. If the search returns nothing "
        "relevant, say you don't have a policy covering it rather than inferring one.\n"
        "You cannot see or change orders. If the customer asks about a specific order, "
        "say that it needs the orders team."),
    tool_names=["search_policies"],
)

SPECIALISTS: dict[str, Agent] = {
    a.name: a for a in (ORDER_AGENT, RETURNS_AGENT, WARRANTY_AGENT, KNOWLEDGE_AGENT)
}

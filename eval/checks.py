"""Deterministic checks applied to a case result.

Every check returns (name, passed, detail). No model judges anything here: a
case either routed where it should, called the tool it should, and produced
the outcome it should, or it did not.
"""
from __future__ import annotations

from typing import Any


def _tool_entry(result, name: str) -> dict | None:
    for call in result.tool_calls:
        if call["name"] == name:
            return call
    return None


def outcome_of(call: dict) -> str:
    res = call["result"]
    if res.get("approval_required"):
        return "approval_required"
    if res.get("error"):
        return "denied"
    return "ok"


def run_checks(case: dict, result, wm=None) -> list[tuple[str, bool, str]]:
    expect: dict[str, Any] = case.get("expect", {})
    checks: list[tuple[str, bool, str]] = []

    if "blocked" in expect:
        checks.append(("blocked", result.blocked == expect["blocked"],
                       f"blocked={result.blocked}"))
    if "block_reason" in expect:
        checks.append(("block_reason", result.block_reason == expect["block_reason"],
                       f"got {result.block_reason!r}"))
    if expect.get("no_tool_calls"):
        checks.append(("no_tool_calls", len(result.tool_calls) == 0,
                       f"{len(result.tool_calls)} tool call(s)"))
    if "route" in expect:
        checks.append(("route", result.route == expect["route"],
                       f"routed to {result.route!r}"))

    if "tool" in expect:
        call = _tool_entry(result, expect["tool"])
        checks.append(("tool_called", call is not None,
                       f"tools: {[c['name'] for c in result.tool_calls] or 'none'}"))
        if call is not None:
            if "outcome" in expect:
                got = outcome_of(call)
                checks.append(("outcome", got == expect["outcome"], f"outcome={got}"))
            if "error" in expect:
                got = call["result"].get("error")
                checks.append(("error_code", got == expect["error"], f"error={got!r}"))

    if "approval_required" in expect:
        got = result.approval_required is not None
        checks.append(("approval_required", got == expect["approval_required"],
                       f"approval_id={result.approval_required}"))

    for needle in expect.get("must_contain", []):
        checks.append((f"contains:{needle[:24]}", needle.lower() in (result.reply or "").lower(),
                       (result.reply or "")[:120]))
    for needle in expect.get("must_not_contain", []):
        blob = (result.reply or "") + " " + str(result.tool_calls)
        checks.append((f"absent:{needle[:24]}", needle.lower() not in blob.lower(),
                       "leaked" if needle.lower() in blob.lower() else "ok"))

    if "working_memory_orders" in expect and wm is not None:
        want = [o.upper() for o in expect["working_memory_orders"]]
        checks.append(("working_memory", wm.orders[:len(want)] == want,
                       f"orders={wm.orders}"))

    return checks

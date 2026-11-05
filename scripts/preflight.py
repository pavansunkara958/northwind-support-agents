#!/usr/bin/env python3
"""Pre-flight checks. Costs a fraction of a cent, saves a confusing hour.

    python scripts/preflight.py
    python scripts/preflight.py --models      list models the endpoint offers

Checks configuration first (free), then — only if a live provider is
configured — that the endpoint actually answers and supports TOOL CALLING.
That last one matters: every specialist in this system works by calling tools,
so an endpoint that serves chat but not tools fails in a way that looks like a
model quality problem for about forty minutes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config  # noqa: E402

OK, WARN, BAD = "ok  ", "warn", "FAIL"
results: list[tuple[str, str, str]] = []


def report(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))
    print(f"  [{level}] {name}" + (f"\n         {detail}" if detail else ""))


def looks_like_placeholder(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return (len(value) < 24
            or any(w in lowered for w in ("paste", "your", "here", "xxx", "changeme",
                                          "replace", "<", ">")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", action="store_true")
    args = parser.parse_args()

    print(f"\nprovider: {config.PROVIDER}\n")

    # 1 — configuration
    print("configuration")
    if config.PROVIDER == "offline":
        report(OK, "offline mode", "deterministic stub; no key required")
    elif config.PROVIDER == "gateway":
        if not config.GATEWAY_BASE_URL:
            report(BAD, "GATEWAY_BASE_URL is not set")
        else:
            report(OK, "GATEWAY_BASE_URL", config.GATEWAY_BASE_URL)
        if looks_like_placeholder(config.GATEWAY_KEY):
            report(BAD, "GATEWAY_KEY looks like a placeholder",
                   f"got {len(config.GATEWAY_KEY)} characters — is .env saved?")
        else:
            report(OK, "GATEWAY_KEY", f"{config.GATEWAY_KEY[:7]}… ({len(config.GATEWAY_KEY)} chars)")
        if not config.GATEWAY_MODEL:
            report(WARN, "GATEWAY_MODEL is not set", "run with --models to list what is offered")
        else:
            report(OK, "GATEWAY_MODEL", config.GATEWAY_MODEL)
    else:
        if looks_like_placeholder(config.OPENAI_API_KEY):
            report(BAD, "OPENAI_API_KEY looks like a placeholder")
        else:
            report(OK, "OPENAI_API_KEY", f"{config.OPENAI_API_KEY[:7]}…")

    # 2 — the signing key
    if config.APPROVAL_KEY_IS_DEFAULT:
        if config.PROVIDER == "offline":
            report(WARN, "APPROVAL_SIGNING_KEY is the dev default",
                   "fine offline; must be a real secret anywhere else")
        else:
            report(BAD, "APPROVAL_SIGNING_KEY is the dev default",
                   "anyone with this repository could mint approval tokens")
    else:
        report(OK, "APPROVAL_SIGNING_KEY", "set from the environment")

    # 3 — thresholds
    if config.REFUND_APPROVAL_THRESHOLD <= 0:
        report(BAD, "REFUND_APPROVAL_THRESHOLD <= 0", "every refund would need approval")
    else:
        report(OK, "refund approval threshold", f"EUR {config.REFUND_APPROVAL_THRESHOLD:.2f}")

    # 4 — tools registered
    from tools import registry  # noqa: E402
    if len(registry.REGISTRY) < 5:
        report(BAD, "tool registry is nearly empty", f"{len(registry.REGISTRY)} registered")
    else:
        report(OK, "tool registry", f"{len(registry.REGISTRY)} tools")
    leaky = [n for n, s in registry.REGISTRY.items() if "customer_id" in s.schema["properties"]]
    if leaky:
        report(BAD, "a tool accepts customer_id as a parameter", ", ".join(leaky))
    else:
        report(OK, "no tool accepts customer_id", "the principal comes from the context")

    if config.PROVIDER == "offline":
        return finish()

    # 5 — the endpoint
    print("\nendpoint")
    from core import llm
    try:
        if args.models:
            models = llm.client().models.list()
            names = sorted(m.id for m in models.data)
            report(OK, f"{len(names)} models offered", "\n         ".join(names[:25]))
            return finish()

        response = llm.complete([{"role": "user", "content": "reply with the word ok"}])
        report(OK, "chat completion", (response.content or "")[:60])
    except Exception as exc:
        report(BAD, "chat completion failed", explain(exc))
        return finish()

    # 6 — tool calling, which is what this system actually needs
    try:
        from tools import registry as reg
        response = llm.complete(
            [{"role": "user", "content": "What is the status of order ORD-10042?"}],
            tools=reg.openai_schemas(["get_order_status"]))
        if response.tool_calls:
            report(OK, "tool calling", f"model called {response.tool_calls[0].name}")
        else:
            report(BAD, "tool calling not used",
                   "the endpoint answered without calling the tool. Every specialist here "
                   "works by calling tools, so this model will not work.")
    except Exception as exc:
        report(BAD, "tool calling failed", explain(exc))

    return finish()


def explain(exc: Exception) -> str:
    text = str(exc)
    if "platform.openai.com" in text:
        return ("The error came from OpenAI, not your gateway — the request went upstream. "
                "Check GATEWAY_BASE_URL and GATEWAY_HEADER_NAME.")
    if "401" in text or "invalid_api_key" in text:
        return ("The endpoint rejected the key. Either the key is wrong, it is inactive, or "
                "it is being sent in the wrong header (see GATEWAY_HEADER_NAME).")
    if "404" in text and "model" in text.lower():
        return "That model name does not exist on this endpoint. Run with --models."
    if "402" in text or "quota" in text.lower():
        return "The account has no credit or quota remaining."
    return text[:400]


def finish() -> int:
    bad = sum(1 for level, _, _ in results if level == BAD)
    warn = sum(1 for level, _, _ in results if level == WARN)
    print(f"\n{len(results) - bad - warn} ok, {warn} warning(s), {bad} failure(s)\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

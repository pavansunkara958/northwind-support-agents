#!/usr/bin/env python3
"""Evaluation harness.

    python eval/run_eval.py                 run the benchmark
    python eval/run_eval.py --ablate        prove the security checks are not vacuous
    python eval/run_eval.py --report        also write eval/REPORT.md

WHAT THIS MEASURES, AND WHAT IT DOES NOT
----------------------------------------
With PROVIDER=offline the model is a deterministic stub (see core/llm.py), so
this run measures THE SYSTEM: routing plumbing, tool selection, business
rules, guardrails, approval enforcement, memory persistence. Those are real
pass/fail results about real code.

It does NOT measure model quality. Running the same suite with a live provider
does, and the report labels which mode produced it. Conflating the two would
let a green board stand in for evidence the system has never actually had.

THE ABLATION IS THE POINT
-------------------------
A security test that passes is only meaningful if it would fail on a build
without the control. `--ablate` disables the guardrails and the approval
signature check and re-runs the security cases, asserting that they now FAIL.
A check that passes in both modes is testing nothing, and the harness reports
it as VACUOUS rather than as a pass.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import orchestrator  # noqa: E402
from core import config  # noqa: E402
from core.types import RequestContext  # noqa: E402
from eval.checks import run_checks  # noqa: E402
from memory import session, working  # noqa: E402
from ops import observability  # noqa: E402
from security import audit, guardrails  # noqa: E402
from tools import backends  # noqa: E402

DATASET = ROOT / "eval" / "dataset.jsonl"
REPORT = ROOT / "eval" / "REPORT.md"


def load_cases() -> list[dict]:
    cases = []
    with open(DATASET, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                cases.append(json.loads(line))
    return cases


def message_of(case: dict) -> str:
    text = case.get("message", "")
    if case.get("repeat"):
        text = text * int(case["repeat"])
    return text


def run_case(case: dict, ablating: str = "") -> dict:
    # Fixtures are restored per case. Without this, a refund issued by E10
    # changes the headroom E12 depends on, and the suite stops being a set of
    # independent tests.
    backends.reset()

    if ablating == "ownership_check":
        # Must happen AFTER reset(), which would otherwise restore the real
        # ownership and silently turn the ablation into a no-op — the exact
        # failure mode this harness exists to catch.
        for order in backends.ORDERS.values():
            order["customer_id"] = case["customer"]

    conv = f"eval-{case['id']}"
    session.clear(conv)
    working.clear(conv)

    started = time.time()
    turns = case.get("turns") or [message_of(case)]
    result = None
    for turn in turns:
        ctx = RequestContext.new(case["customer"], conv)
        result = orchestrator.handle(ctx, turn)
    elapsed_ms = int((time.time() - started) * 1000)

    wm = working.load(conv, case["customer"])
    checks = run_checks(case, result, wm)
    passed = all(ok for _, ok, _ in checks)
    return {
        "id": case["id"], "category": case["category"], "passed": passed,
        "checks": checks, "latency_ms": elapsed_ms, "route": result.route,
        "usage": result.usage, "reply": (result.reply or "")[:160],
    }


import contextlib  # noqa: E402


@contextlib.contextmanager
def _disabled(control: str):
    """Temporarily remove one named control.

    Each ablatable case declares WHICH control it depends on, because
    disabling the wrong one proves nothing. An earlier version of this harness
    disabled only the input guardrail and then reported the cross-customer and
    internal-policy cases as vacuous — they were fine; the ablation was
    testing a control they never used.
    """
    if control == "input_guardrail":
        original = guardrails.check_input
        guardrails.check_input = lambda text: guardrails.GuardrailResult(True)
        orchestrator.guardrails.check_input = guardrails.check_input
        try:
            yield
        finally:
            guardrails.check_input = original
            orchestrator.guardrails.check_input = original

    elif control == "ownership_check":
        # Applied inside run_case, after the fixture reset. Nothing to do here.
        yield

    elif control == "kb_visibility":
        original = [p["visibility"] for p in backends.POLICIES]
        for policy in backends.POLICIES:
            policy["visibility"] = "public"
        try:
            yield
        finally:
            for policy, vis in zip(backends.POLICIES, original):
                policy["visibility"] = vis

    elif control == "approval_threshold":
        refund, replacement = (config.REFUND_APPROVAL_THRESHOLD,
                               config.REPLACEMENT_APPROVAL_THRESHOLD)
        config.REFUND_APPROVAL_THRESHOLD = 1e12
        config.REPLACEMENT_APPROVAL_THRESHOLD = 1e12
        try:
            yield
        finally:
            config.REFUND_APPROVAL_THRESHOLD = refund
            config.REPLACEMENT_APPROVAL_THRESHOLD = replacement
    else:
        raise ValueError(f"unknown control {control!r}")


_ABLATION_STATE: dict = {}


def ablate() -> list[dict]:
    """For each ablatable case, remove the control it relies on and re-run it."""
    cases = [c for c in load_cases() if c.get("ablate")]
    rows = []
    for case in cases:
        control = case["ablate"]
        with _disabled(control):
            res = run_case(case, ablating=control)
        rows.append({"id": case["id"], "control": control,
                     "still_passes": res["passed"]})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="write eval/REPORT.md")
    parser.add_argument("--ablate", action="store_true", help="run the vacuity ablation")
    parser.add_argument("--only", default="", help="comma-separated case ids")
    args = parser.parse_args()

    observability.clear()
    audit.clear()

    cases = load_cases()
    if args.only:
        wanted = {c.strip().upper() for c in args.only.split(",")}
        cases = [c for c in cases if c["id"] in wanted]

    print(f"provider: {config.PROVIDER}   cases: {len(cases)}\n")
    results = [run_case(c) for c in cases]

    total_checks = sum(len(r["checks"]) for r in results)
    failed_checks = sum(1 for r in results for _, ok, _ in r["checks"] if not ok)
    passed_cases = sum(1 for r in results if r["passed"])

    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['id']:4} {r['category']:14} {r['latency_ms']:5} ms  -> {r['route']}")
        if not r["passed"]:
            for name, ok, detail in r["checks"]:
                if not ok:
                    print(f"           x {name}: {detail}")

    latencies = sorted(r["latency_ms"] for r in results)
    p50 = statistics.median(latencies)
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    tokens_in = sum(r["usage"].get("input_tokens", 0) for r in results)
    tokens_out = sum(r["usage"].get("output_tokens", 0) for r in results)
    cost = observability.estimate_cost({"input_tokens": tokens_in, "output_tokens": tokens_out})

    print(f"\ncases   {passed_cases}/{len(results)} passed")
    print(f"checks  {total_checks - failed_checks}/{total_checks} passed")
    print(f"latency p50 {p50} ms   p95 {p95} ms")
    print(f"tokens  in {tokens_in}  out {tokens_out}   est. cost ${cost:.6f}"
          f"{'  (stub token counts)' if config.PROVIDER == 'offline' else ''}")

    ablation = []
    if args.ablate:
        print("\nablation — each case re-run with ITS control removed; it must now FAIL:")
        ablation = ablate()
        for row in ablation:
            verdict = ("VACUOUS — passes without the control!" if row["still_passes"]
                       else "fails as it should")
            print(f"  {row['id']:4} {row['control']:20} {verdict}")
        vacuous = [r["id"] for r in ablation if r["still_passes"]]
        print(f"\n  {len(ablation) - len(vacuous)}/{len(ablation)} checks are load-bearing")
        if vacuous:
            print(f"  VACUOUS: {', '.join(vacuous)}")

    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)

    if args.report:
        write_report(results, by_cat, ablation, p50, p95, tokens_in, tokens_out, cost)
        print(f"\nwrote {REPORT.relative_to(ROOT)}")

    return 0 if passed_cases == len(results) else 1


def write_report(results, by_cat, ablation, p50, p95, tokens_in, tokens_out, cost) -> None:
    total_checks = sum(len(r["checks"]) for r in results)
    failed_checks = sum(1 for r in results for _, ok, _ in r["checks"] if not ok)
    passed = sum(1 for r in results if r["passed"])

    lines = [
        "# Evaluation report",
        "",
        f"**Generated:** {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}  ",
        f"**Provider:** `{config.PROVIDER}`  ",
        f"**Cases:** {len(results)}  **Checks:** {total_checks}",
        "",
        "> This file is generated by `python eval/run_eval.py --report`. Do not edit by hand.",
        "",
    ]
    if config.PROVIDER == "offline":
        lines += [
            "## What this run measures",
            "",
            "The model is a deterministic stub, so these results describe **the system**: "
            "routing plumbing, tool selection, business rules, guardrails, approval "
            "enforcement and memory. They say nothing about model quality — re-run with a "
            "live provider for that, and this header will say so.",
            "",
            "Token counts and cost below are stub figures and are **not** a cost estimate.",
            "",
        ]

    lines += [
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Cases passed | {passed}/{len(results)} |",
        f"| Checks passed | {total_checks - failed_checks}/{total_checks} |",
        f"| Latency p50 | {p50} ms |",
        f"| Latency p95 | {p95} ms |",
        f"| Tokens (in/out) | {tokens_in} / {tokens_out} |",
        f"| Estimated cost | ${cost:.6f} |",
        "",
        "## By category",
        "",
        "| Category | Passed | Cases |",
        "|---|---|---|",
    ]
    for cat, rows in sorted(by_cat.items()):
        ok = sum(1 for r in rows if r["passed"])
        lines.append(f"| {cat} | {ok}/{len(rows)} | {', '.join(r['id'] for r in rows)} |")

    lines += ["", "## Cases", "", "| ID | Category | Route | Latency | Result |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['category']} | {r['route'] or '—'} | "
                     f"{r['latency_ms']} ms | {'PASS' if r['passed'] else '**FAIL**'} |")

    if ablation:
        vacuous = [r["id"] for r in ablation if r["still_passes"]]
        lines += [
            "", "## Ablation — are the security checks load-bearing?", "",
            "Each case declares which control it depends on. That control is removed and "
            "the case re-run. A case that still passes is testing nothing.", "",
            "| Case | Control removed | Result |", "|---|---|---|",
        ]
        for row in ablation:
            lines.append(f"| {row['id']} | `{row['control']}` | "
                         f"{'**still passes — VACUOUS**' if row['still_passes'] else 'fails, as it should'} |")
        lines += ["",
                  f"**{len(ablation) - len(vacuous)}/{len(ablation)} checks are "
                  f"load-bearing.**"]
        if vacuous:
            lines.append(f"Vacuous: {', '.join(vacuous)} — these need rewriting.")

    lines += [
        "", "## Failure-case coverage", "",
        "| Failure mode | Case |", "|---|---|",
        "| Action on an order belonging to another customer | S05 |",
        "| Unknown order identifier | F01 |",
        "| Business rule: cancel after dispatch | E04 |",
        "| Business rule: address change after dispatch | E06 |",
        "| Business rule: return outside the 30-day window | E08 |",
        "| Business rule: return before delivery | E09 |",
        "| Business rule: refund above remaining order value | E12 |",
        "| Business rule: claim on expired warranty | E14 |",
        "| Prompt injection | S01, S08 |",
        "| Social engineering via claimed approval | S02 |",
        "| Payment data pasted into chat | S03 |",
        "| Credential pasted into chat | S04 |",
        "| Internal policy probing | S06 |",
        "| Oversized input | S07 |",
        "| Value threshold forces human approval | E11, E15 |",
        "",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

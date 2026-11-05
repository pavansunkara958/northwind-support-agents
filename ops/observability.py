"""Tracing: OpenTelemetry GenAI-shaped spans, emitted as JSONL.

Attribute names follow the OpenTelemetry GenAI semantic conventions
(`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`) so that swapping
this file for a real OTLP exporter is a transport change, not a remodelling
exercise. Writing JSONL keeps the repository runnable with no collector.

Two things recorded here that are easy to leave out and expensive to miss:

1. **Guardrail allows, not just blocks.** Recording only blocks gives a
   denominator of zero, and the block *rate* is the signal — a guardrail whose
   false-positive rate is climbing looks identical to one that is working if
   you only count blocks.
2. **Routing decisions.** If routing silently degrades, error rate, latency
   and cost all *improve*, because fewer tools get called. Every RED metric
   gets better while the product gets worse, so the distribution of routes is
   its own signal.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid

from core import config

_lock = threading.Lock()
_local = threading.local()


def _emit(record: dict) -> None:
    with _lock:
        with open(config.TRACE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextlib.contextmanager
def span(name: str, *, trace_id: str | None = None, kind: str = "internal", **attrs):
    parent = getattr(_local, "span_id", None)
    trace = trace_id or getattr(_local, "trace_id", None) or new_trace_id()
    span_id = uuid.uuid4().hex[:16]

    prev_trace, prev_span = getattr(_local, "trace_id", None), parent
    _local.trace_id, _local.span_id = trace, span_id

    started = time.time()
    record = {"name": name, "trace_id": trace, "span_id": span_id,
              "parent_span_id": parent, "kind": kind, "start": started,
              "attributes": dict(attrs)}
    try:
        yield record
        record["status"] = "ok"
    except Exception as exc:
        record["status"] = "error"
        record["attributes"]["error.type"] = type(exc).__name__
        raise
    finally:
        record["duration_ms"] = int((time.time() - started) * 1000)
        _local.trace_id, _local.span_id = prev_trace, prev_span
        _emit(record)


def model_span(record: dict, *, model: str, usage: dict, agent: str) -> None:
    record["attributes"].update({
        "gen_ai.system": config.PROVIDER,
        "gen_ai.request.model": model,
        "gen_ai.agent.name": agent,
        "gen_ai.usage.input_tokens": usage.get("input_tokens", 0),
        "gen_ai.usage.output_tokens": usage.get("output_tokens", 0),
    })


def estimate_cost(usage: dict) -> float:
    return round(
        usage.get("input_tokens", 0) / 1_000_000 * config.PRICE_PER_MTOK["input"]
        + usage.get("output_tokens", 0) / 1_000_000 * config.PRICE_PER_MTOK["output"], 6)


def read_traces() -> list[dict]:
    try:
        with open(config.TRACE_LOG, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def clear() -> None:
    open(config.TRACE_LOG, "w").close()

"""Typed tool registry.

A tool declares its parameters with Python type hints. The registry derives the
JSON schema the model sees AND validates arguments at call time from the same
source, so the two cannot drift apart.

Three properties worth stating because they are security properties, not
conveniences:

1. `customer_id` is never a tool parameter. Tools read the authenticated
   principal from `RequestContext`. The conversation cannot name the principal.
2. Validation happens before the function body runs. A malformed or
   hallucinated argument produces a structured error the agent can recover
   from, not an exception that ends the turn.
3. Every invocation is written to the audit log, including the ones that fail
   validation — a rejected call is exactly the kind of event worth keeping.
"""
from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin, get_type_hints

from core.types import RequestContext

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


class ToolInputError(ValueError):
    """Raised when arguments fail validation. Converted to a structured result."""


@dataclass
class ToolSpec:
    name: str
    description: str
    func: Callable
    schema: dict
    risk: str          # low | medium | high | critical
    writes: bool
    agent: str


REGISTRY: dict[str, ToolSpec] = {}


def _schema_for(func: Callable) -> dict:
    hints = get_type_hints(func)
    sig = inspect.signature(func)
    props: dict[str, Any] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if pname == "ctx":
            continue
        ann = hints.get(pname, str)
        origin = get_origin(ann)
        if origin is not None:  # Optional[X] / X | None
            args = [a for a in get_args(ann) if a is not type(None)]
            ann = args[0] if args else str
        props[pname] = {"type": _JSON_TYPES.get(ann, "string")}
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def _coerce(value: Any, expected: type, field: str) -> Any:
    if expected is float and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if expected is int and isinstance(value, bool):
        raise ToolInputError(f"'{field}' must be an integer, got a boolean")
    if expected is int and isinstance(value, float) and value.is_integer():
        return int(value)
    if expected is str and not isinstance(value, str):
        raise ToolInputError(f"'{field}' must be a string")
    if expected in (int, float) and isinstance(value, str):
        try:
            return expected(value)
        except ValueError:
            raise ToolInputError(f"'{field}' must be a number, got {value!r}") from None
    if not isinstance(value, expected):
        raise ToolInputError(f"'{field}' must be {expected.__name__}, got {type(value).__name__}")
    return value


def tool(*, risk: str, writes: bool, agent: str, description: str):
    """Register a function as a tool.

    `risk` and `writes` are metadata the policy layer and the audit log read.
    They are declared next to the implementation so a new tool cannot be added
    without someone stating what it can do.
    """

    def decorator(func: Callable) -> Callable:
        schema = _schema_for(func)
        REGISTRY[func.__name__] = ToolSpec(
            name=func.__name__, description=description, func=func,
            schema=schema, risk=risk, writes=writes, agent=agent,
        )
        return func

    return decorator


def openai_schemas(names: list[str]) -> list[dict]:
    out = []
    for n in names:
        spec = REGISTRY[n]
        out.append({"type": "function", "function": {
            "name": spec.name, "description": spec.description,
            "parameters": spec.schema}})
    return out


def validate(name: str, arguments: dict) -> dict:
    spec = REGISTRY[name]
    hints = get_type_hints(spec.func)
    props = spec.schema["properties"]

    unknown = set(arguments) - set(props)
    if unknown:
        raise ToolInputError(f"unknown argument(s): {', '.join(sorted(unknown))}")

    missing = [r for r in spec.schema["required"] if r not in arguments
               or arguments[r] in ("", None)]
    if missing:
        raise ToolInputError(f"missing required argument(s): {', '.join(missing)}")

    clean: dict[str, Any] = {}
    for key, value in arguments.items():
        expected = hints.get(key, str)
        origin = get_origin(expected)
        if origin is not None:
            args = [a for a in get_args(expected) if a is not type(None)]
            expected = args[0] if args else str
        clean[key] = _coerce(value, expected, key)
    return clean


def invoke(name: str, arguments: dict, ctx: RequestContext) -> dict:
    """Validate, execute, audit. Always returns a JSON-serialisable dict."""
    from security.audit import record

    started = time.time()
    if name not in REGISTRY:
        record(ctx, tool=name, arguments=arguments, outcome="unknown_tool", risk="n/a")
        return {"error": "unknown_tool", "message": f"no tool named {name}"}

    spec = REGISTRY[name]
    try:
        clean = validate(name, arguments)
    except ToolInputError as exc:
        record(ctx, tool=name, arguments=arguments, outcome="invalid_arguments",
               risk=spec.risk, detail=str(exc))
        return {"error": "invalid_arguments", "message": str(exc)}

    try:
        result = spec.func(ctx=ctx, **clean)
    except Exception as exc:  # a tool must never take the process down
        record(ctx, tool=name, arguments=clean, outcome="tool_exception",
               risk=spec.risk, detail=f"{type(exc).__name__}: {exc}")
        return {"error": "tool_failed", "message": "the backend did not respond as expected"}

    outcome = "approval_required" if result.get("approval_required") else (
        "denied" if result.get("error") else "ok")
    record(ctx, tool=name, arguments=clean, outcome=outcome, risk=spec.risk,
           detail=result.get("message"), writes=spec.writes,
           duration_ms=int((time.time() - started) * 1000))
    return result


def as_json(result: dict) -> str:
    return json.dumps(result, default=str)

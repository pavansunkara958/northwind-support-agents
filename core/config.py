"""Runtime configuration.

Provider resolution order:
  1. PROVIDER env var ("offline" | "openai" | "gateway")
  2. If unset: "gateway" when GATEWAY_KEY is present, "openai" when
     OPENAI_API_KEY is present, otherwise "offline".

`offline` is the default on a fresh clone ON PURPOSE. A reviewer must be able
to clone this repository and run the full security and tool-layer test suite
without obtaining an API key. See README "Running without a key".
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # python-dotenv is optional; the system runs without it
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover - optional dependency
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


OPENAI_API_KEY = _env("OPENAI_API_KEY")
GATEWAY_BASE_URL = _env("GATEWAY_BASE_URL")
GATEWAY_KEY = _env("GATEWAY_KEY")
GATEWAY_HEADER_NAME = _env("GATEWAY_HEADER_NAME", "Authorization")
GATEWAY_MODEL = _env("GATEWAY_MODEL")


def _default_provider() -> str:
    if GATEWAY_KEY and GATEWAY_BASE_URL:
        return "gateway"
    if OPENAI_API_KEY:
        return "openai"
    return "offline"


PROVIDER = _env("PROVIDER", _default_provider()).lower()

MODEL_ROUTER = _env("MODEL_ROUTER", GATEWAY_MODEL or "gpt-4o-mini")
MODEL_SPECIALIST = _env("MODEL_SPECIALIST", GATEWAY_MODEL or "gpt-4o-mini")

# --- data locations ---------------------------------------------------------
DATA_DIR = Path(_env("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_DB = DATA_DIR / "sessions.db"

LOG_DIR = Path(_env("LOG_DIR", str(ROOT / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
TRACE_LOG = LOG_DIR / "traces.jsonl"
AUDIT_LOG = LOG_DIR / "audit.jsonl"

# --- policy thresholds ------------------------------------------------------
# Money moves and goods ship above these lines only with a verified approval.
REFUND_APPROVAL_THRESHOLD = float(_env("REFUND_APPROVAL_THRESHOLD", "100.00"))
REPLACEMENT_APPROVAL_THRESHOLD = float(_env("REPLACEMENT_APPROVAL_THRESHOLD", "150.00"))
APPROVAL_TTL_SECONDS = int(_env("APPROVAL_TTL_SECONDS", "1800"))

# Signing key for approval tokens. In production this is a KMS-held key; here
# it is an env secret. A default exists so the test suite runs on a fresh
# clone, and `scripts/preflight.py` fails loudly if it is still the default
# while PROVIDER is not offline.
APPROVAL_SIGNING_KEY = _env("APPROVAL_SIGNING_KEY", "dev-only-insecure-signing-key")
APPROVAL_KEY_IS_DEFAULT = APPROVAL_SIGNING_KEY == "dev-only-insecure-signing-key"

MAX_TOOL_ITERATIONS = int(_env("MAX_TOOL_ITERATIONS", "4"))
MAX_INPUT_CHARS = int(_env("MAX_INPUT_CHARS", "4000"))

# Cost table (USD per 1M tokens). Used by the evaluation harness; approximate
# by design, and labelled as such in the report.
PRICE_PER_MTOK = {
    "input": float(_env("PRICE_IN", "0.15")),
    "output": float(_env("PRICE_OUT", "0.60")),
}


def summary() -> dict:
    return {
        "provider": PROVIDER,
        "model_router": MODEL_ROUTER if PROVIDER != "offline" else "offline-stub",
        "model_specialist": MODEL_SPECIALIST if PROVIDER != "offline" else "offline-stub",
        "refund_approval_threshold": REFUND_APPROVAL_THRESHOLD,
        "approval_key_is_default": APPROVAL_KEY_IS_DEFAULT,
    }

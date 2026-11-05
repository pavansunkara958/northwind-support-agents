"""Working memory: bounded structured state per conversation.

What the transcript cannot do well:

  * answer "which order are we talking about?" without re-reading every turn,
  * survive being trimmed,
  * be queried by anything other than a model.

So the facts the system extracts are kept here, in a fixed shape, with hard
caps. Bounded is the whole point: an unbounded "memory" is just a second
transcript with extra steps, and it will grow until it costs more to carry
than the conversation it describes.

Eviction is least-recently-referenced, not FIFO. In a support conversation the
order the customer keeps coming back to is the one that matters, and a plain
FIFO queue would drop exactly that one in a long thread.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field

from core import config

MAX_ORDERS = 5
MAX_INTENTS = 10
MAX_FACTS = 12

ORDER_RE = re.compile(r"\bORD-\d{4,}\b", re.I)


@dataclass
class WorkingMemory:
    conversation_id: str
    customer_id: str = ""
    orders: list[str] = field(default_factory=list)       # most-recent first
    intents: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    pending_approval_id: str | None = None
    updated_at: float = 0.0

    # --- mutation -------------------------------------------------------
    def touch_order(self, order_id: str) -> None:
        if not order_id:
            return
        oid = order_id.upper()
        if oid in self.orders:
            self.orders.remove(oid)
        self.orders.insert(0, oid)
        del self.orders[MAX_ORDERS:]

    def note_intent(self, intent: str) -> None:
        if intent:
            self.intents.append(intent)
            del self.intents[:-MAX_INTENTS]

    def set_fact(self, key: str, value) -> None:
        self.facts[key] = value
        if len(self.facts) > MAX_FACTS:
            # Drop the oldest inserted key; dicts preserve insertion order.
            oldest = next(iter(self.facts))
            self.facts.pop(oldest)

    @property
    def current_order(self) -> str | None:
        return self.orders[0] if self.orders else None

    def observe(self, text: str) -> None:
        """Extract structure from a message. Deterministic, no model call."""
        for match in ORDER_RE.finditer(text or ""):
            self.touch_order(match.group(0))

    def as_prompt_context(self) -> str:
        """Compact rendering injected into the specialist's system prompt."""
        bits = []
        if self.orders:
            bits.append(f"Orders discussed (most recent first): {', '.join(self.orders)}")
        if self.pending_approval_id:
            bits.append(f"An approval request is pending: {self.pending_approval_id}")
        if self.facts:
            bits.append("Known: " + "; ".join(f"{k}={v}" for k, v in self.facts.items()))
        return "\n".join(bits)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.SESSION_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS working_memory (
        conversation_id TEXT PRIMARY KEY, payload TEXT, updated_at REAL)""")
    conn.commit()
    return conn


def load(conversation_id: str, customer_id: str = "") -> WorkingMemory:
    conn = _conn()
    row = conn.execute("SELECT payload FROM working_memory WHERE conversation_id=?",
                       (conversation_id,)).fetchone()
    conn.close()
    if not row:
        return WorkingMemory(conversation_id=conversation_id, customer_id=customer_id)
    data = json.loads(row[0])
    data.setdefault("customer_id", customer_id)
    return WorkingMemory(**data)


def save(wm: WorkingMemory) -> None:
    wm.updated_at = time.time()
    conn = _conn()
    conn.execute("INSERT INTO working_memory VALUES (?,?,?) "
                 "ON CONFLICT(conversation_id) DO UPDATE SET payload=?, updated_at=?",
                 (wm.conversation_id, json.dumps(asdict(wm)), wm.updated_at,
                  json.dumps(asdict(wm)), wm.updated_at))
    conn.commit()
    conn.close()


def clear(conversation_id: str) -> None:
    conn = _conn()
    conn.execute("DELETE FROM working_memory WHERE conversation_id=?", (conversation_id,))
    conn.commit()
    conn.close()

"""Session memory: the transcript, persisted in SQLite.

This is one of the two memory tiers, and it answers "what was said".
`memory/working.py` answers "what is known". They are separate because they
have different shapes, different growth rates and different lifetimes:

  * the transcript grows linearly with the conversation and is replayed into
    the model, so it must be trimmed;
  * the working set is bounded, structured and queryable, and the pending
    approval inside it must survive a session expiry.

A pending approval kept only in the transcript would be lost on expiry, and
the customer would be waiting on a request that no longer exists.
"""
from __future__ import annotations

import json
import sqlite3
import time

from core import config

MAX_REPLAY_TURNS = 12


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.SESSION_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT,
        agent TEXT,
        ts REAL NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv ON messages(conversation_id, id)")
    conn.commit()
    return conn


def append(conversation_id: str, role: str, content: str, agent: str = "") -> None:
    conn = _conn()
    conn.execute("INSERT INTO messages (conversation_id, role, content, agent, ts) "
                 "VALUES (?,?,?,?,?)",
                 (conversation_id, role, content, agent, time.time()))
    conn.commit()
    conn.close()


def history(conversation_id: str, limit: int = MAX_REPLAY_TURNS) -> list[dict]:
    """Return the most recent messages, oldest first.

    Trimmed rather than summarised: for a support conversation the last dozen
    turns carry the context and the rest is noise. Summarisation would add a
    model call to every turn to compress text the working memory already holds
    in structured form.
    """
    conn = _conn()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? "
        "ORDER BY id DESC LIMIT ?", (conversation_id, limit)).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def turn_count(conversation_id: str) -> int:
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'",
                     (conversation_id,)).fetchone()[0]
    conn.close()
    return int(n)


def clear(conversation_id: str) -> None:
    conn = _conn()
    conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    conn.commit()
    conn.close()


def export(conversation_id: str) -> str:
    return json.dumps(history(conversation_id, limit=10_000), indent=2)

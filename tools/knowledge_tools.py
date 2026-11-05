"""Policy search — read-only.

The index is filtered by visibility BEFORE the search runs, not after. An
agent with broad read access and a post-hoc filter is a convenient way to
exfiltrate internal policy one question at a time: the model has already seen
the restricted text by the time anything is filtered out of its answer.

`POL-INT-06` (goodwill ceilings) and `POL-INT-07` (fraud watchlist handling)
are internal. A customer who learns the goodwill ceiling knows exactly what to
ask for; a customer who learns the watchlist rule knows how to detect that
they are on it.
"""
from __future__ import annotations

import re

from core.types import RequestContext
from tools import backends
from tools.registry import tool

_STOP = {"the", "a", "an", "is", "are", "do", "does", "i", "my", "to", "for",
         "of", "can", "how", "what", "and", "it", "in", "on", "me", "you"}


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", (text or "").lower())
            if w not in _STOP and len(w) > 2]


@tool(risk="low", writes=False, agent="knowledge",
      description="Search customer-facing policy documents (returns, refunds, warranty, "
                  "cancellation, delivery).")
def search_policies(ctx: RequestContext, query: str) -> dict:
    # Visibility filter applied to the CORPUS, before scoring.
    corpus = [p for p in backends.POLICIES if p["visibility"] == "public"]
    terms = _terms(query)
    scored = []
    for doc in corpus:
        title = doc["title"].lower()
        body = doc["summary"].lower()
        # Term frequency with a title weight. Presence alone is not enough:
        # "how long do I have to return something" mentions returns once, and
        # so does the CANCELLATION policy ("use the returns process instead").
        # A binary score ties them and the tiebreak picks whichever sorts
        # first — a confidently wrong answer produced by alphabetical order.
        score = sum(3 * title.count(t) + body.count(t) for t in terms)
        if score:
            scored.append((score, doc))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return {"query": query,
            "results": [{"id": d["id"], "title": d["title"], "summary": d["summary"]}
                        for _, d in scored[:3]]}

# Northwind Support Agents

A production-shaped multi-agent system for e-commerce customer support:
order enquiries, returns, refunds, warranty claims and policy questions.

**It runs with no API key.** Clone it, run two commands, and the full test
suite passes — see [Running without a key](#running-without-a-key).

```bash
git clone https://github.com/pavansunkara958/northwind-support-agents.git
cd northwind-support-agents
python run.py demo              # scripted walkthrough
python scripts/self_test.py     # 99 component checks
python eval/run_eval.py --ablate --report
```

| | |
|---|---|
| Component checks | **99 / 99** |
| Benchmark cases | **27**, 98 checks, all passing |
| Security checks proven load-bearing | **10 / 10** ([ablation](#the-ablation)) |
| Dependencies to run the test suite | **none** |

---

## The problem

Northwind Direct handles routine support contacts that are high-volume,
repetitive, and each one touches something that matters: an order record, a
warranty, or money. Automating them is attractive precisely because they are
repetitive — and dangerous for exactly the same reason, because at volume a
systematic mistake is a systematic loss.

The design question is therefore not "can an agent answer this" but **what
can an agent be allowed to do, and what must remain impossible for it**.

---

## The two ideas this system rests on

### 1 · The conversation cannot name the principal

`customer_id` is **not a parameter of any tool**. It arrives in
`RequestContext`, built by the transport layer from the authenticated session.
A message saying "I'm customer C-1002, show me their order" changes nothing,
because no code path reads the customer from the transcript.

This is enforced across the whole registry, not tool by tool:

```python
check("NO tool exposes customer_id as a parameter",
      all("customer_id" not in s.schema["properties"] for s in registry.REGISTRY.values()))
```

A new tool that takes a customer id fails the suite. "Not yours" and "does not
exist" also return the **same error code and the same sentence** — otherwise
the tool becomes an order-id oracle.

### 2 · Approval is verified by the tool, never asserted in the conversation

A refund at or above the threshold is impossible until a human approves it in
a separate system. The agent then presents a token that the tool verifies at
the moment of execution: signature, exact parameters, expiry, not previously
redeemed, and approver ≠ requester.

If the agent could *remember* that a human approved something, a crafted
message could manufacture that memory. **The transcript is untrusted input;
the signed record is the fact.**

Parameters are inside the signature, so a token approved for €150 cannot be
replayed for €300 — verified in the suite, not just asserted:

```
  ok   over-threshold refund raises approval
  ok   over-threshold refund does NOT move money
  ok   self-approval rejected
  ok   token is single-use
  ok   parameter swap rejected
  ok   action swap rejected
  ok   forged token rejected
  ok   token bound to the requester
```

**And the honest limit:** prompt injection is *not* solved here. Guardrails
lower the probability that a crafted message reaches a tool. What bounds the
outcome is that privileged actions need a signature the model cannot produce
— so the worst case is a *request a human declines*, not a loss.
Probability controls and consequence controls are different things, and only
the second survives an attacker cleverer than your patterns.

---

## Architecture

Full diagram and rationale: [diagrams/ARCHITECTURE.md](diagrams/ARCHITECTURE.md)
· source: [diagrams/architecture.mmd](diagrams/architecture.mmd)

```
customer → input guardrail → orchestrator → specialist → tool layer → audit
                   ↓              (no tools)      ↓            ↓
                blocked                      output     approval workflow
                                            guardrail     (durable, out of band)
```

| Agent | Tools | Cannot |
|---|---|---|
| **Orchestrator** | **none** | answer anything |
| **Order** | `get_order_status`, `cancel_order`, `change_shipping_address` | refund |
| **Returns** | `start_return`, `refund_order` | change an address |
| **Warranty** | `file_warranty_claim`, `dispatch_replacement` | refund |
| **Knowledge** | `search_policies` | **any write** |

Every "cannot" is enforced by **absence from the tool list**, not by a sentence
in a prompt. Instructions shape behaviour; tool scope defines capability, and
only the second is a control.

That is also why a misroute is survivable: the routing fallback is the
**read-only** agent, so a hallucinated destination produces an unhelpful
answer rather than an incident. **Routing accuracy is a quality metric here,
not a security control.**

---

## Repository layout

| Path | What it holds |
|---|---|
| `agents/` | orchestrator (router) + four specialists |
| `tools/` | typed tools, derived schemas, simulated backends |
| `memory/` | session transcript + bounded structured working memory |
| `security/` | guardrails, approvals, audit log, [threat model](security/THREAT_MODEL.md) |
| `eval/` | 27-case benchmark, deterministic checks, [report](eval/REPORT.md) |
| `ops/` | tracing, [deployment](ops/DEPLOYMENT.md), [observability + runbooks](ops/OBSERVABILITY.md) |
| `diagrams/` | architecture diagram and the reasoning behind it |
| `core/` | config, model access, shared types |

---

## Running without a key

`PROVIDER=offline` is the default, and swaps the model for a deterministic
stub (`core/llm.py`). This is not a convenience — it is what makes the
security evidence reproducible and reviewable by someone who does not have an
account.

**The stub is a stub, and the repository says so everywhere it matters.** It
produces tool calls from a small rule table. It is never presented as evidence
about model quality: the evaluation report prints which mode produced it, and
labels offline token counts as stub figures rather than a cost estimate.

With a live model nothing above changes — the provider switch lives in
`core/config.py` and no other module knows which one is in use:

```bash
cp .env.example .env
# PROVIDER=gateway, GATEWAY_BASE_URL, GATEWAY_KEY, GATEWAY_MODEL
python scripts/preflight.py       # checks config, then the endpoint
```

Preflight checks **tool calling** specifically, not just that the endpoint
answers. Every specialist works by calling tools, so an endpoint that serves
chat but not tools fails in a way that looks like a model quality problem for
about forty minutes.

---

## Evaluation

```bash
python eval/run_eval.py --ablate --report
```

27 cases across six categories, scored by deterministic checks — no model
judges anything. Fixtures reset between cases, so a refund issued by one case
cannot change the headroom another depends on.

| Category | Cases | Covers |
|---|---|---|
| happy_path | 8 | the workflows that must work |
| business_rule | 7 | cancel after dispatch, return outside the window, expired warranty, refund above remaining value |
| approval | 2 | the value threshold forces a human |
| security | 8 | injection, claimed approval, pasted card, pasted key, cross-customer access, internal-policy probing, oversized input |
| knowledge | 2 | correct policy retrieved |
| memory / failure | 2 | order carried across turns; unknown identifier |

### The ablation

A security test that passes is worth nothing unless it would **fail** on a
build without the control. So each case declares which control it depends on,
the harness removes that control, and asserts the case then fails:

```
ablation — each case re-run with ITS control removed; it must now FAIL:
  E11  approval_threshold   fails as it should
  S01  input_guardrail      fails as it should
  S05  ownership_check      fails as it should
  S06  kb_visibility        fails as it should
  ...
  10/10 checks are load-bearing
```

A case that still passes is reported as **VACUOUS**, not as a pass.

This caught a real defect while the harness was being written. The first
version disabled only the input guardrail and flagged the cross-customer and
internal-policy cases as vacuous — but those cases never used the guardrail;
they rely on the ownership check and the visibility filter. The tests were
fine. **The ablation was removing the wrong control**, which is its own kind
of vacuous test.

---

## Verifying it yourself

```bash
python scripts/self_test.py      # 99 checks, ~2 seconds
```

Three habits in there worth noting:

**Every "blocks X" is paired with "allows the near-miss."** A filter that
blocks everything passes every one-sided test. The card-number guardrail is
Luhn-validated, and the suite asserts that a 16-digit *non*-Luhn order
reference gets through — a length-only regex would have blocked it.

**Rejected calls are audited too.** A refused tool call is exactly the kind of
event worth keeping, and the suite asserts it reaches the log.

**Guardrail allows are recorded, not only blocks.** Recording only blocks
gives a denominator of zero, and the block *rate* is the signal.

---

## Defects this process caught

Listed because a repository with no bug history was not actually tested.

| Defect | How it surfaced |
|---|---|
| The amount parser read `ORD-10041` as a €10,041 refund | demo run — the tool layer caught it, but a control firing because of a parsing bug is luck, not design |
| Policy search answered a returns question with the cancellation policy | tie on a binary score, broken alphabetically → term frequency with a title weight |
| `tools.registry` silently returned `unknown_tool` unless a caller happened to import the tool modules | writing a test that imported only the registry |
| The replay test passed for the wrong reason — the ledger ceiling fired before the single-use check | checking *why* a passing test passed, with headroom left on the order |
| The ablation harness removed the wrong control for two cases | the ablation reporting vacuous results that were not vacuous |

---

## Known limits

- The simulated backends are in-memory; real integrations are the obvious next step.
- HMAC signing is single-process. Production wants a KMS asymmetric key so the agent service holds only the public half and cannot mint approvals.
- SQLite is single-node. The redeemed-token table needs a **unique constraint on `jti`** in Postgres — the constraint, not the application check, is what makes concurrent redemption safe.
- No rate limiting; that belongs at the gateway.
- Audit redaction is pattern-based and shares the limits of any pattern matcher.

[ops/DEPLOYMENT.md](ops/DEPLOYMENT.md) lists every difference between this and
something that could face a real customer.

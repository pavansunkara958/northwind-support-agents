# Observability

Two sinks, deliberately separate:

| | `logs/traces.jsonl` | `logs/audit.jsonl` |
|---|---|---|
| Answers | why was this slow / wrong | who did what to whose money |
| Retention | 30 days | years |
| Sampling | yes, under load | **never** |
| Redaction | arguments trimmed | arguments redacted |

Merging them means either paying trace prices for audit retention, or sampling
away the record of a refund. Neither is acceptable, so they are two files.

---

## Spans

Named per OpenTelemetry GenAI semantic conventions, so swapping JSONL for an
OTLP exporter is a transport change rather than a remodelling exercise.

| Span | Key attributes |
|---|---|
| `support.request` | `customer.id`, `conversation.id`, `request.id` |
| `guardrail.input` | `guardrail.allowed`, `guardrail.category` |
| `orchestrator.route` | `route.destination`, `route.confidence` |
| `agent.<name>.model` | `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| `tool.<name>` | `tool.name`, `tool.risk` |
| `guardrail.output` | `guardrail.allowed`, `guardrail.category` |

Spans nest under one trace per request, so a slow turn can be attributed to
the model, a tool or the guardrails without guessing.

---

## Metrics

### RED — the easy ones

| Metric | Alarm |
|---|---|
| Error rate | > 2% over 5 min |
| p95 request latency | > 12 s over 10 min |
| Cost per conversation | > 2× the 7-day median |

### Behavioural — the ones that catch what RED cannot

| Metric | Alarm | Why RED misses it |
|---|---|---|
| **Routing mix vs baseline** | distribution shift > 15% | when routing degrades, fewer tools run: error rate, latency *and* cost all **improve** |
| **Guardrail block rate** | ±50% vs 7-day baseline | a block costs less than a real answer, so false positives look like efficiency |
| **Tool refusal rate by code** | ±40% per code | a business rule changed, or a backend is lying |
| **Escape rate** (handoff to a human) | > 20% | the honest measure of whether it works |
| **Approval raise rate** | ±50% | a threshold change nobody announced, or a pricing change |

### Security — should be zero

| Metric | Alarm |
|---|---|
| `approval_invalid_signature` | **any** → P1 |
| `approval_already_redeemed` | **any** → P1 |
| `approval_parameter_mismatch` | **any** → P1 |
| `approval_requester_mismatch` | **any** → P1 |
| `tool_not_available` rate | > 1% → P3 (routing problem, not an attack) |

These are alarmed at **any** occurrence precisely because they should never
occur. A control you are certain cannot fail is a control nobody monitors, and
the first time it fails you want a page, not a dashboard nobody opens.

**Guardrail spans record allows as well as blocks.** Recording only blocks
gives a denominator of zero, and the *rate* is what tells you whether the
guardrail is working or quietly rejecting real customers.

---

## Dashboard layout

```
row 1   requests/min · error rate · p95 latency · cost per conversation
row 2   ROUTING MIX (stacked area, with the baseline band overlaid)
row 3   guardrail block rate by category   |   tool outcome by code
row 4   approval raise / grant / reject    |   SECURITY EVENTS (should be flat zero)
row 5   escape rate                        |   reopen rate
```

Row 2 is second, above the guardrail panels, because routing drift is the
failure mode most likely to go unnoticed for a week.

Row 4's right panel is a flat line at zero in normal operation. That is the
point: a panel that is always empty is a panel whose first non-zero value is
unmistakable.

---

## Reading the logs

```bash
# every span for one request
jq 'select(.trace_id=="<id>")' logs/traces.jsonl

# routing distribution
jq -r 'select(.name=="orchestrator.route").attributes."route.destination"' \
   logs/traces.jsonl | sort | uniq -c

# guardrail block rate (needs the allows, which is why they are recorded)
jq -r 'select(.name|startswith("guardrail")) | .attributes."guardrail.allowed"' \
   logs/traces.jsonl | sort | uniq -c

# every write, with its outcome
jq 'select(.writes==true) | {ts, tool, outcome, customer_id}' logs/audit.jsonl

# security events — expect nothing
jq 'select(.event=="security")' logs/audit.jsonl
```

---

## Runbooks

| Alarm | First action | Then |
|---|---|---|
| Security event, any | **Freeze**: set `AGENT_ENABLED=false`. Do not debug a live system that may be under attack | Pull the audit entries for that `conversation_id`; the token claims are in the event detail |
| Routing mix shifted | Compare the route distribution to the baseline, then sample 20 traces from the over-weighted route | If the model version changed, roll back and run the eval suite before re-deploying |
| Guardrail blocks spiked | Check the category breakdown | Single category → a new attack pattern *or* a false-positive regression. Read 10 blocked messages before changing anything |
| Guardrail blocks collapsed | Verify the guardrail is still wired in — `self_test.py` catches a disconnected one | A quiet guardrail and a bypassed guardrail look identical on a block-count graph |
| p95 latency up, error rate flat | Check tool spans before model spans | Tool latency dominates the slow tail |
| Approvals queueing | This is a staffing problem, not a system problem | Replicas make the queue longer, not shorter |
| Cost per conversation up | Check mean tool iterations and transcript length | A loop that is retrying costs twice before it fails |

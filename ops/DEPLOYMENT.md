# Deployment

Docker Compose for the reference deployment; the same image runs on ECS
Fargate, Cloud Run or a VM.

---

## Run it

```bash
cp .env.example .env          # offline by default — no key needed
docker compose up --build
docker compose exec app python run.py demo
docker compose exec app python scripts/self_test.py
```

To use a live model, set `PROVIDER` and the key in `.env` and restart. Nothing
else changes: the provider switch is in `core/config.py` and no other module
knows which one is in use.

---

## Secrets

| Rule | Why |
|---|---|
| Every secret arrives as an environment variable | no key in the image, the repo or a config file |
| `.env` is git-ignored; `.env.example` holds placeholders only | the file with the real values is never the file that is committed |
| `APPROVAL_SIGNING_KEY` is a **separate** secret from the model key | the model key leaking must not also mean approvals can be forged |
| Signing key rotation invalidates outstanding tokens | acceptable: TTL is 30 minutes, so the blast radius of a rotation is one approval cycle |
| `scripts/preflight.py` refuses a non-offline start on the default signing key | the dev default exists so a fresh clone runs; shipping it is the failure this catches |

In production the signing key is a KMS-held asymmetric key: the approval
service signs, the agent service holds only the public half, and a compromise
of the agent service cannot mint approvals. The HMAC here is the
single-process form of the same design — stated because the difference
matters and glossing over it would be dishonest.

---

## Scaling

The app is **stateless per request**. State lives in SQLite here and in
Postgres in production; scale by running more replicas behind the load
balancer.

| Dimension | Approach | Limit to watch |
|---|---|---|
| Throughput | horizontal replicas, no session affinity needed | model provider rate limits bite first |
| Latency | router and specialists can use different models — the router is a short classification and does not need the large model | tool latency, not model latency, dominates the slow tail |
| Cost | `MODEL_ROUTER` is set separately from `MODEL_SPECIALIST` for exactly this reason | token growth from transcript replay — bounded at 12 turns |
| Storage | approval records and the audit log grow forever by design | traces expire at 30 days; **audit records do not** |

Two things do **not** scale by adding replicas:

- **The redeemed-token table** must be strongly consistent across replicas, or
  the same approval can be redeemed twice on two nodes. SQLite is
  single-node; Postgres with a unique constraint on `jti` is the production
  form, and the constraint — not the application check — is what makes it
  safe.
- **Human approvers.** The approval SLA is a staffing decision. If approvals
  queue, the system is not slow, it is understaffed, and adding replicas makes
  the queue longer.

---

## Rollback

```bash
docker compose down
git checkout <previous-tag>
docker compose up --build -d
```

| Step | Detail |
|---|---|
| Trigger | eval suite red, security event rate non-zero, or error rate > 2% for 5 min |
| Kill switch | `AGENT_ENABLED=false` → every request returns the human-handoff message. Faster than a rollback; use it first |
| Target | the previous image tag, always kept warm |
| Time | < 60 s for the flag, < 5 min for the image |
| **Not rolled back** | the database. Approval records and the audit log are append-only and survive deliberately — a rollback must never erase the record of a refund |

**Rehearse the kill switch before launch, not after.** A switch that has never
been pulled is a hypothesis.

### Schema changes

Migrations are additive-only across a rollback boundary: add a column, deploy,
backfill, then start reading it in a later release. A migration that drops or
renames makes the previous image unable to start, which converts a rollback
into an outage.

---

## Pre-deploy gate

```bash
python scripts/self_test.py          # 99 component checks
python eval/run_eval.py --ablate     # 27 cases + the vacuity ablation
python scripts/preflight.py          # config sanity; endpoint check if live
```

All three must pass. The ablation is in the gate because a security suite that
has quietly become vacuous passes just as loudly as one that works.

---

## Production differences

This repository is a reference implementation. Deploying it for real means:

| Here | Production |
|---|---|
| SQLite | Postgres, unique constraint on `jti` |
| Simulated backends | real order, payment and warranty services |
| HMAC signing key in env | KMS asymmetric key; agent holds only the public half |
| Approval via a CLI | ServiceNow / Slack, authenticated separately |
| JSONL traces | OTLP to the collector; audit to WORM storage |
| No rate limiting | gateway-level, per customer and per IP |
| No PII retention policy | transcripts expire; audit records do not |

None of these change the architecture. All of them must be done before this
handles a real customer.

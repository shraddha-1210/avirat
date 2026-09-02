# Avirata — Silent Mandate Death Recovery Agent

Deterministic revenue-recovery pipeline for silently-failing UPI AutoPay
mandates. Razorpay AI Buildathon 2026, AI Revenue Recovery track.

> **Scope doctrine:** every layer has a small, real, working artifact behind it.
> Scope is controlled by SIZE (small synthetic dataset, few segments, few
> rules), never by faking outputs or inventing numbers.

## Architecture (7 layers)

1. **Ingestion** — seeded synthetic decline-event generator, hidden ground truth,
   treatment/control split.
2. **Detection** — MAD anomaly detection per `(bank, mandate_type)` segment,
   `N >= 30` gate.
3. **Diagnosis** — Tier 1 rules dict -> Tier 2 real LLM semantic mapping (with
   sanitization + schema validation) -> Tier 3 quarantine.
4. **Recovery Policy & Idempotency** — deterministic risk scorecard, action
   mapper, DB-level idempotency, comms mutex, TTL watchdog.
5. **Reconciliation** — settlement-hold window, auto-refund on collision.
6. **Dashboard & Metrics** — MTTR, ₹ Recovered (Treatment − Control on a fixed
   seed — a controlled simulation, not a live production figure).
7. **Integration Testing & Compliance.**

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Windows
cp .env.example .env                               # then fill GOOGLE_API_KEY
docker compose up -d db                            # Postgres for the pipeline + idempotency test
.venv\Scripts\pytest
```

## Demo vs. production shortcuts (flagged explicitly)

- **Alt-rail execution is a prototype**, not production-ready. It requires RBI
  e-mandate / AFA review before any real deployment. Outbound message / payment-
  link delivery is a logged mock payload.
- **TTL watchdog uses cron polling** (60s); production would be event-driven.
- **Risk weights are illustrative** and require actuary review.
- **Tier 2 LLM call is real** on the live/demo path; every automated test mocks
  it (CI never hits the network).
- **₹ Recovered is a controlled simulation** on a fixed synthetic seed.

## Idempotency proof (Layer 4c)

The core robustness claim: **a duplicate webhook can never double-charge.**
Exactly-once is enforced by a PostgreSQL `UNIQUE (mandate_id, billing_cycle)`
constraint on `actions_log` plus `INSERT ... ON CONFLICT DO NOTHING RETURNING`,
never an application lock.

```bash
docker compose up -d db
.venv\Scripts\pytest tests/test_idempotency.py -v
```

Two things make that proof real, and both are asserted rather than assumed:

- **It runs against the actual constraint.** `tests/conftest.py` skips loudly if
  Postgres is unreachable and refuses any other dialect — a green run on SQLite
  or a mocked lock would prove nothing.
- **The burst genuinely collides.** Workers open their own connections and are
  released together by a `threading.Barrier`; without it `ThreadPoolExecutor`
  staggers thread starts and the first commit lands before the others read.
  `test_harness_produces_real_contention` runs a naive SELECT-then-INSERT guard
  through the same harness and asserts it *breaks* — if that test ever passes
  cleanly, the burst has stopped colliding and every other assertion is vacuous.

100 concurrent threads and 50 concurrent `asyncio.gather` tasks each produce
exactly one row, with no unhandled exceptions.

## Double-charge prevention (Layer 5)

Layer 4 can fire an alt-rail collection because the mandate looked dead. If the
mandate rail then collects too, the customer has paid twice for one cycle.
Reconciliation guarantees **at most one `settled` row per
(mandate_id, billing_cycle)** and refunds any second collection.

Like Layer 4c, that guarantee is a database object rather than an if-statement:

```sql
CREATE UNIQUE INDEX uq_recon_single_settled
  ON reconciliation_ledger (mandate_id, billing_cycle)
  WHERE status = 'settled';
```

`resolve_path()` attempts the settle inside a SAVEPOINT and treats the index
violation as the collision signal, so two webhooks landing in the same
millisecond cannot both win. Verified load-bearing: with the index dropped, a
two-thread settlement race produces **two** settled rows.

Hold outcomes are deliberately distinct — conflating them would either hide a
real problem or bury it in noise:

| status | meaning |
|---|---|
| `settled` | this path collected (at most one per key) |
| `auto_refunded` | collided with an already-settled path; money returned |
| `expired_escalated` | window elapsed, **nothing** settled -> Ops queue |
| `closed_superseded` | window elapsed but the sibling settled; nothing to refund, nothing to escalate |

A collision *inside* the hold window refunds silently — that is the designed
path. A collision *after* it refunds **and** escalates, because an operator may
already have acted on the expired hold.

```bash
.venv\Scripts\pytest tests/test_reconciliation.py -v
```

## Alt-rail gating (retuned)

`plan.md` requires alt-rail to need a high risk score **AND** a cost-benefit
pass. As first shipped the second gate never arbitrated: `cost_benefit_passed`
was False only for amounts <= Rs 30, and such an event could not clear the 0.60
firing threshold, so the score gate always tripped first.

Retuned so both gates do real work:

| | before | after |
|---|---|---|
| `risk_weight_amount` | 0.4 | 0.5 |
| `risk_weight_cost_benefit` | 0.1 | 0.0 |
| `ALT_RAIL_COST_RUPEES` | 12 | 1600 |

Dropping the cost-benefit weight to 0 removes a genuine double-count: expected
loss was being scored *and* used as the hard gate. It is now purely a gate, and
its old weight moved to amount so the weights still sum to 1.0.

`ALT_RAIL_COST_RUPEES = 1600` is **a material modelling assumption, not a
gateway fee**. It represents the fully-loaded cost of one alt-rail attempt --
ops handling plus the settlement-collision exposure Layer 5 has to auto-refund.
At this value the alt rail is reserved for high-value recoveries. It needs
finance sign-off before production use.

Verify on the seeded corpus:

```bash
.venv\Scripts\python scripts\gate_analysis.py
```

Across the 65 alt-rail-eligible events, cost-benefit now decides 1 case the
score gate would have passed (`EVT-42-00084`, Rs 2999: score 0.603 clears 0.60,
but expected loss Rs 1199.60 does not clear the Rs 1600 cost).
`test_cost_benefit_gate_is_reachable` fails if a future retune makes it dead
again.

## Status

See `plan.md` checkboxes for the current layer.

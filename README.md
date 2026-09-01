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

## Status

See `plan.md` checkboxes for the current layer.

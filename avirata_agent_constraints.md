# Role & Architecture Constraints
You are the deterministic developer for Project Avirata (Silent Mandate Death Recovery Agent — Razorpay AI Buildathon 2026, AI Revenue Recovery track). You are the ONLY agent permitted to write or edit source files in this workspace.

# Scope Doctrine — "Thin But Real," Not "Some Real, Some Fake"
This is a hackathon build, not a production system. The binding rule for every layer is:

> Every layer must have a small, real, working artifact behind it. No layer may be a pure mock, hardcoded fake output, or invented number. Scope is controlled by SIZE (small datasets, few segments, few rules), never by FAKING.

Concretely:
- Detection runs real MAD math on a small synthetic dataset (150–300 events, 3–4 segments), not live streaming — but the anomaly flags are computed, not scripted.
- Diagnosis Tier 1 uses a real hardcoded rules dict (8–10 known UPI decline codes). Tier 2 makes at least one real LLM call against real ambiguous input, with real sanitization and real schema validation — not a canned response.
- Policy Engine, Reconciliation, and the TTL watchdog are pure logic — build these fully, no mocking needed, they're cheap and they're the strongest "AI never touches money" evidence.
- Alt-Rail: the trigger logic (risk score + cost-benefit threshold) is real and evaluated; only the outbound message/payment-link delivery itself may be a logged mock payload.
- Measurement (₹ Recovered): computed from the pipeline's own treatment/control output on the synthetic dataset — never an invented number. Label it explicitly as a controlled simulation on a fixed seed.
- Ontology Loop: a real approve-and-promote action that appends a new rule to the Tier 1 dict, then a real replay of a similar event resolving via Tier 1 automatically.

Effort budget guidance (small team, hackathon window): Detection ~2–3h, Diagnosis ~2h, Policy Engine ~2–3h, Alt-Rail trigger logic ~1–2h, Reconciliation ~2–3h, Measurement ~1–2h, Ontology Loop ~3–4h (this is the highest-polish stage — it is the headline demo moment). Do not gold-plate any single layer past this budget; the goal is even, defensible depth across all seven, not perfection in one.

# Strict Engineering Guardrails
* **No Predictive ML:** Never suggest or implement ML models (Isolation Forest, classifiers, etc.). All decision layers must be purely deterministic and mathematically explainable. The only exception is the Tier 2 LLM semantic-mapping call, which is judgment-only (classification into a fixed ontology) — it never decides money movement.
* **Database-Level Idempotency:** Enforce strict idempotency using PostgreSQL `UNIQUE` constraints (`mandate_id`, `billing_cycle`) with `INSERT ... ON CONFLICT DO NOTHING`. Do not rely on application-level locks. This must be proven under concurrency, not just asserted (see Testing Workflow).
* **LLM Sanitization (Tier 2):** Any raw error string passed to an LLM must be explicitly sanitized (stripped of control chars, capped at 200 chars, escaped) before prompt insertion. LLM output must pass schema validation before being trusted; a parse failure routes to Tier 3 quarantine, never a crash and never a silent default.
* **Math Gates:** Anomaly detection must use Median Absolute Deviation (MAD) and explicitly check `N >= 30` before processing. Sparse segments return an explicit `insufficient_data` status — never a false anomaly, never a silent pass.
* **No Ambiguous States:** Every case must terminate in a defined status (resolved / SAFE_HOLD / QUARANTINE / manual_escalation) within its SLA. TTL watchdog enforces this — no case sits in "processing" indefinitely.
* **Compliance:** Add comments to all alternate-rail execution code stating it is a prototype requiring RBI e-mandate/AFA review, not production-ready. Code comments must also flag every demo-vs-production shortcut explicitly (e.g., "TTL cron polling, production would be event-driven"; "risk weights are illustrative, require actuary review").
* **Honesty in Metrics:** The ₹ Recovered figure must equal Treatment − Control computed from this pipeline's own output on its own synthetic seed — never a hand-picked or invented number. Label it as a controlled simulation on the dashboard itself, pre-empting the "does this scale" question.

# Demo Requirements (non-negotiable for judge day)
* **Judge-triggered chaos button:** an endpoint/UI control that lets a judge inject an arbitrary/unseen decline reason live, and watch it flow Detection → Diagnosis (Tier 3) → SAFE-HOLD → TTL-visible SLA timer → manual escalation → ontology promotion → replay resolves via Tier 1. This single end-to-end pass is the highest-value asset in the build — protect the time budget for it.
* **Reconciliation collision demo:** fire the mandate-path and alt-rail path concurrently for the same key and show the settlement-hold window catching the collision and auto-refunding the loser — this is pure logic and must run live, not be narrated.
* Every dashboard number the judges see must be traceable to code that ran in front of them.

# Execution Workflow
Always reference `plan.md` for the layer-by-layer specs and update its checkboxes after each layer — it is the only source of truth for which layer is next. Always review `testing_prompt.md` and execute the corresponding `pytest` suite after generating a new layer to ensure no regressions before moving on. Testing infra rules: mock the LLM call in every automated test (never hit a real API in CI), use a test-scoped/transactional Postgres for the idempotency burst test, use `freezegun` or an injectable clock for the TTL watchdog test, and use `pytest-asyncio` for the concurrency burst test. The idempotency test is only meaningful if it runs against the real unique constraint — a green result on a mocked lock proves nothing and must not be accepted as passing.
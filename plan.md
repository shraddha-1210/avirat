**Silent Mandate Death Recovery Agent** system. I understand:

✅ **Core System**: 6-layer deterministic financial recovery system (Detection → Diagnosis → Risk Scoring → Recovery Policy → Reconciliation → Dashboard)

✅ **Non-Negotiables**:

- Deterministic, explainable decisions only (no ML models except for Tier 2 LLM semantic mapping, with strict output validation)
- Input sanitization for untrusted error strings before LLM processing
- Concrete idempotency via DB constraint or Redis lock (not just prose)
- Compliance framing: alt-rail as prototype requiring review, not production-ready

✅ **Key Guardrails**:

- MAD-based anomaly detection with `N ≥ 30` minimum sample gate
- 3-tier diagnosis (Known → Ambiguous+LLM → Quarantine)
- Revenue-risk scorecard (deterministic weights, never fire alt-rail on urgency alone)
- TTL watchdog preventing infinite "processing" state
- Settlement-hold window with auto-refund on collision
- Comms mutex to suppress duplicate reminders

✅ **Hackathon Scope**: Build layers 2–3 (detection + diagnosis) and 4c (idempotency) fully; simplify 4a/4b, 5, and 6 where time-bounded.

---

**Before I create the implementation plan, I need to clarify a few things:** 

Analyzing your answers...

Perfect. Now let me create a comprehensive implementation plan for a 7-day hackathon build with Python + FastAPI + PostgreSQL, targeting all layers end-to-end with robustness as a core principle. 

Created memory file /memories/session/plan.md

I see — no workspace is open yet, so I can't save to session memory until you create one. Let me present the comprehensive plan here:

---

## Plan: Silent Mandate Death Recovery Agent — 7-Day Implementation

**TL;DR**

Build a 6-layer deterministic financial recovery system in Python/FastAPI + PostgreSQL + React. Detect mandate failures early with robust MAD anomaly detection, diagnose root cause via rule lookup + LLM semantic mapping (with strict input sanitization), score revenue risk deterministically, fire bounded recovery actions, and enforce idempotency via PostgreSQL unique constraints. Core robustness tested: inject 100 concurrent duplicate events and verify zero double-charges. All decisions logged for audit trail; dashboard shows MTTR, ₹ Recovered (Treatment vs Control delta), and Ops escalation queue.

---

### **Steps**

- [x] **Phase 1: Foundation & Mock Data (Day 1 — 4–5 hours)**  — done; 7/7 ingestion tests pass (testing.md Phase 1)

1. FastAPI skeleton + PostgreSQL schema (mandates, decline_events, actions_log, reconciliation_ledger, quarantine_queue, comms_state)
2. SQLAlchemy ORM models with explicit type hints
3. Layer 1: `layers/ingestion.py` — synthetic decline-event generator with deterministic seed, ground-truth hidden from downstream layers, `split_treatment_control()` 50/50 split

- [x] **Phase 2: Detection (Days 2–3 — 6–8 hours)**  — done; 25/25 tests pass (testing.md Phase 2). Caveat cleared in Phase 4: `store.py` writes now run against live Postgres 16 (`docker compose up -d db`). 4. Layer 2: `layers/detection.py` — MAD-based anomaly per `(bank, mandate_type)` segment, 7-day rolling window, N ≥ 30 minimum gate (explicit `insufficient_data` return, never silent pass), 3×MAD threshold logged numerically 5. Ingestion → Detection pipeline: `/api/events/ingest` webhook fetches segment history, calls `check_anomaly()`, writes to `detected_anomalies` table

- [x] **Phase 3: Diagnosis (Days 3–4 — 6–8 hours)**  — done; 41/41 tests pass (testing.md Phase 3). Tier 2 runs on Google Gemini (`gemini-3.5-flash-lite`) and the REAL call is now verified: `scripts/tier2_calibration.py` scores 6/6 mappable strings correct and 3/3 novel strings quarantined against the seeded 240-event corpus. 6. Layer 3: `layers/diagnosis.py` — Tier 1 rule-based dict lookup (instant, no LLM), Tier 2 LLM semantic mapping with **input sanitization** (strip control chars, cap 200 chars, escape before prompt), schema validation on LLM output (parse error → Tier 3, not crash), confidence ≥ 85% required to resolve Tier 2 7. Tier 3 quarantine path: writes raw input + tier attempted to `quarantine_queue` for Ops review and future ontology promotion 8. Anomaly → Diagnosis pipeline logs all three tiers with explicit numeric confidence scores

- [x] **Phase 4: Recovery Policy & Idempotency (Days 4–5 — 8–10 hours) — _CORE ROBUSTNESS FOCUS_**  — done; 83/83 tests pass (42 new). Idempotency proven against real Postgres 16: 100-thread and 50-task async bursts each yield exactly 1 row, with a negative-control test asserting the harness genuinely collides. Follow-up applied: the alt-rail cost-benefit gate was unreachable dead code (the score gate always tripped first), so it was retuned — `risk_weight_amount` 0.4->0.5, `risk_weight_cost_benefit` 0.1->0.0 (expected loss was double-counted as both a score component and the gate), `ALT_RAIL_COST_RUPEES` 12->1600. Cost-benefit now independently blocks 1 of 65 alt-rail-eligible events; see `scripts/gate_analysis.py`. Rs 1600 is a fully-loaded-cost assumption requiring finance sign-off. 9. Layer 4a: `layers/recovery_policy.py` — deterministic weighted risk scorecard (urgency + reliability + amount tier − alt-rail cost-benefit), w1=0.2, w2=0.3, w3=0.4, w4=0.1 (tunable) 10. Layer 4b: Action mapper — `bank_downtime` → RETRY (2x), `insufficient_funds` → NUDGE_BALANCE (WhatsApp mock), high risk + Tier 2/3 → ALT_RAIL (UPI Intent Link), low risk → SAFE_HOLD (quiet retry) 11. **Layer 4c (CRITICAL)**: `fire_action_idempotent()` — PostgreSQL unique constraint on `(mandate_id, billing_cycle)` in actions_log; explicit guard clause checking for existing action before INSERT; handle unique_violation exception as no-op, return False; log "idempotent guard" on duplicate 12. Layer 4d: `layers/comms_orchestrator.py` — single owner of all messaging per `(mandate_id, billing_cycle)`, if alt-rail fires set `communication_state.alt_rail_live`, standard reminder job checks flag before sending 13. Layer 4e: `tasks/ttl_watchdog.py` — background cron (60s interval, noted in README as demo-scale; production would be event-driven), finds status='processing' > 5 min, marks as `manual_escalation`, escalates to Ops queue

- [ ] **Phase 5: Reconciliation (Day 5 — 4–6 hours)** 14. Layer 5: `layers/reconciliation.py` — settlement-hold window (5 min), mandate-path + alt-rail-path tracked in `reconciliation_ledger` with compound key `(mandate_id, billing_cycle)` 15. Webhook handler on mandate result: if alt-rail also resolved within settlement-hold window, auto-refund the second path, mark `reconciliation_status='auto_refund_processed'` 16. Settlement-hold expiry (5-min cron): neither path resolved → mark `reconciliation_status='expired_escalated'`, escalate to Ops (safety valve for stuck cases)

- [ ] **Phase 6: Dashboard & Metrics (Day 6 — 5–7 hours)** 17. Layer 6: `layers/metrics.py` — compute MTTR per tier, ₹ Recovered = Treatment alt-rail/nudge sum − Control baseline churn, recovery rate (% resolved < 24h), Ops escalations by type 18. React dashboard (`src/Dashboard.tsx`, `src/components/`):
    - MTTR gauge (grouped by Tier 1/2/3)
    - **"₹ Recovered" bar chart with explicit label: "Controlled simulation on fixed synthetic seed — not live production"**
    - Ops queue table sortable by age, shows mandate_id, billing_cycle, escalation_reason
    - Layer 2 anomaly stats: segment-wise MAD values, flags in last 24h
    - Tier 3 quarantine backlog count + sample cases 19. Audit & compliance logging: every decision point logs timestamp, inputs, rule applied, output; endpoint `/api/audit/decision/{mandate_id}/{billing_cycle}` returns full trace

- [ ] **Phase 7: Integration Testing & Compliance (Days 6–7 — 4–6 hours)** 20. Test scenarios:
    - **Happy path**: Tier 1 error → RETRY → resolves, LOW_RISK → no alt-rail
    - **Degradation**: Tier 2 diagnosed insufficient_funds → NUDGE + HIGH_RISK → alt-rail fires
    - **Hallucinated code**: unknown error → Tier 3 QUARANTINE → Ops queue
    - **Idempotency stress**: 100 concurrent identical events → only 1 action fires (_this is your idempotency proof_)
    - **TTL watchdog**: inject stuck processing record → watchdog finds, escalates 21. Compliance README: 2-sentence disclaimer on alt-rail (prototype, requires RBI e-mandate/AFA review before production) 22. Code comments flag demo vs. production shortcuts ("WhatsApp logged, not real API," "TTL cron not event-driven," "risk weights require actuary review in production")

---

### **Relevant Files** (to create)

**Backend Structure**

- app.py — FastAPI entry point, routes: /api/events/ingest, /api/dashboard/summary, /api/audit/decision, /api/health
- models.py — SQLAlchemy ORM: Mandate, DeclineEvent, DetectedAnomaly, Diagnosis, Action, ActionsLog, ReconciliationLedger, QuarantineQueue, OpsEscalationQueue, CommunicationState
- config.py — tunable parameters (MAD_THRESHOLD=3.0, MIN_SAMPLE_SIZE=30, MAX_RETRIES=2, SETTLEMENT_HOLD_SECONDS=300, etc.)
- layers/ingestion.py — `generate_events(n, seed)` → DataFrame, `split_treatment_control(df, seed)` → (treatment_df, control_df)
- layers/detection.py — `check_anomaly(segment_history, new_value, ...) → AnomalyResult(is_anomaly, mad, threshold, sample_size, reason)`
- layers/diagnosis.py — `diagnose_tier1()`, `diagnose_tier2_llm()` with sanitization + schema validation, `diagnose_tier3_quarantine()`, DiagnosisResult dataclass
- layers/recovery_policy.py — `score_recovery_risk()`, `map_diagnosis_to_action()`, `fire_action_idempotent()` with explicit guard clause
- layers/comms_orchestrator.py — `send_nudge()`, `suppress_standard_reminder()` mutex logic
- layers/reconciliation.py — `handle_mandate_webhook()`, `settle_expired_holds()`, auto-refund on collision
- layers/metrics.py — `compute_metrics()` → MTTR, revenue recovery, escalation stats
- tasks/ttl_watchdog.py — background 60s cron loop, finds stuck processing > 5 min
- requirements.txt — FastAPI, SQLAlchemy, psycopg2, pydantic, requests, openai (or mock LLM client)

**Frontend (React + TypeScript)**

- src/App.tsx — routing, API calls
- src/Dashboard.tsx — main layout: MTTR gauge, recovery chart, ops queue, audit trail
- src/components/MTTRGauge.tsx, RecoveryChart.tsx, OpsQueue.tsx, AuditTrail.tsx

**Database (PostgreSQL)**

- Schema with explicit `UNIQUE (mandate_id, billing_cycle)` constraint on `actions_log` table

**Tests (pytest)**

- tests/test_detection.py — Layer 2: sufficient_data, insufficient_data (N < 30), MAD calculation match
- tests/test_diagnosis.py — Layer 3: Tier 1 instant, Tier 2 LLM validation, input sanitization, parse failure → Tier 3, confidence threshold
- tests/test_idempotency.py — **Layer 4c stress test**: insert identical (mandate_id, billing_cycle) twice → second is no-op; 100 concurrent events → only 1 action
- tests/test_reconciliation.py — Layer 5: webhook handling, auto-refund on collision, settlement-hold expiry
- tests/test_e2e.py — end-to-end scenarios

---

### **Verification**

| Layer                  | Test                                                                      | Outcome                            |
| ---------------------- | ------------------------------------------------------------------------- | ---------------------------------- |
| **2 (Detection)**      | Segment N=29 → returns `insufficient_data`, never flags                   | No false alarms on sparse data     |
| **2 (Detection)**      | Manually compute MAD for 30+ synthetic values, verify layer matches       | Math is correct, defendable in Q&A |
| **3 (Diagnosis)**      | Tier 1 known code → instant, no LLM call                                  | Fast path works                    |
| **3 (Diagnosis)**      | LLM returns invalid JSON → Tier 3 quarantine, no crash                    | Defensive parsing                  |
| **3 (Diagnosis)**      | Input injection attempt → stripped before LLM call                        | No prompt-injection risk           |
| **4a (Scorer)**        | Risk score ≤ threshold **or** cost-benefit fails → low risk (no alt-rail) | Scorecard enforced                 |
| **4c (Idempotency)**   | Insert `(mandate_id, billing_cycle)` twice → second is no-op              | No double-charge                   |
| **4c (Idempotency)**   | **100 concurrent inserts (thread pool) → only 1 succeeds**                | **Stress-tested under load**       |
| **4e (TTL)**           | Stuck processing > 5 min → auto-escalated                                 | No infinite "processing" state     |
| **5 (Reconciliation)** | Alt-rail + mandate both resolve < 5 min → auto-refund fires               | Collision handled                  |
| **5 (Reconciliation)** | Settlement-hold expires, neither resolved → escalated                     | No ambiguous states                |
| **6 (Metrics)**        | ₹ Recovered exactly = Treatment − Control sum                             | Honest metrics, no fudging         |
| **6 (Dashboard)**      | All gauges/tables load without error                                      | User-facing complete               |
| **7 (Compliance)**     | README has AFA disclaimer, code flags demo shortcuts                      | Transparent build                  |

---

### **Decisions**

- **Idempotency**: PostgreSQL unique constraint on actions_log `(mandate_id, billing_cycle)`, not Redis — simpler audit trail in DB
- **LLM input sanitization**: First-class guardrail — strip before prompt, validate schema after, reject parse failures (never coerce)
- **Alt-rail trigger**: Requires **both** high risk_score **AND** cost-benefit check (expected_loss > alt_rail_cost) — never on urgency alone
- **Settlement-hold collision**: Auto-refund if both paths resolve within 5 min; if neither resolves, escalate (no ambiguous states)
- **TTL watchdog**: Demo-scale cron polling (60s); production would be event-driven — stated explicitly in code comments + README
- **Dashboard metric**: "₹ Recovered" explicitly labeled as controlled simulation (fixed seed) to pre-empt "does this scale" skepticism
- **Compliance framing**: Alt-rail is prototype requiring RBI e-mandate/AFA review, not production-ready — stated in README + code comments

---

### **Further Considerations**

1. **LLM API source**: Will you use OpenAI API (requires key management), local Claude, or deterministic mock Tier 2 for demo? For hackathon, mock + deterministic path is sufficient; does the judge expect real LLM calls?
2. **Retry parametrization**: Should max retries, backoff strategy (linear/exponential) be stored in mandate config or baked into Layer 4b `map_diagnosis_to_action()`?
3. **WhatsApp fidelity**: Mock-to-DB (current plan) or sandbox Business API integration if time permits?

---

**Ready to refine any phase or dive into implementation once you clarify the three questions above and open a workspace.**

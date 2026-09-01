# Test & Validation Spec — Mandate Recovery Agent

> Pair this with the CI system prompt. For each phase: Input → Processing → Expected Output → Validation Criteria. Copilot should generate `pytest` assertions directly off the "Validation Criteria" rows — they're written to be near-literal test assertions, not vague descriptions.

**Test infra notes (needed to hit the <10s runtime budget):**

- Mock the LLM call in every test — never hit a real API in CI.
- Use a test-scoped Postgres (or transactional rollback per test) so the idempotency burst test doesn't leak rows across tests.
- Use `freezegun` or an injectable clock for the TTL watchdog test — don't actually sleep 10 minutes.
- Use `pytest-asyncio` for the 50-request concurrency burst.

---

## `plan.md` tracking template (the CI prompt reads this to find "most recently completed phase")

```markdown
# Build Plan

- [ ] Phase 1: Ingestion & Mock Data
- [ ] Phase 2: Detection (MAD anomaly)
- [ ] Phase 3: Diagnosis (3-tier) —
- [ ] Phase 4: Policy & Idempotency
- [ ] Phase 5: Reconciliation
- [ ] Phase 6: Dashboard/Metrics
```

Keep this file updated after each layer — it's the only thing telling Copilot which test file to generate next.

---

## Phase 1 — Ingestion Tests

**Test 1: hidden ground-truth label present**

- **Input:** `generate_events(n=100, seed=42)`
- **Processing:** function builds a synthetic DataFrame; `true_cause` is generated as a hidden column, drawn from the fixed ontology, never exposed to downstream detection/diagnosis payloads.
- **Expected output:** DataFrame with a `true_cause` column, values ∈ `{bank_downtime, insufficient_funds, unknown, ...}` (your fixed ontology set).
- **Validation criteria:**
  - `assert 'true_cause' in df.columns`
  - `assert df['true_cause'].isin(ONTOLOGY_SET).all()`

**Test 2: seeded split is reproducible and leak-free**

- **Input:** same `df`, `split_treatment_control(df, seed=42)` called twice.
- **Processing:** deterministic partition into Treatment/Control (e.g. 50/50); the payload object handed to Layers 2–4 must exclude `true_cause` — it's only rejoined later for the Layer 6 measurement step.
- **Expected output:** two DataFrames (`treatment_df`, `control_df`) with identical row-to-group assignment across both runs; the downstream-facing payload has no `true_cause` column.
- **Validation criteria:**
  - `assert treatment_df.index.tolist() == treatment_df_run2.index.tolist()` (same seed → same split)
  - `assert 'true_cause' not in payload_df.columns`

---

## Phase 2 — Detection Tests

**Test 1: sample-size gate at N=29**

- **Input:** segment history of exactly 29 decline-event values for one `(bank, mandate_type)` segment.
- **Processing:** `check_anomaly()` counts sample size before computing MAD; gate requires N ≥ 30.
- **Expected output:** `AnomalyResult(is_anomaly=False, status='insufficient_data', sample_size=29, mad=None)` — no exception, no false anomaly.
- **Validation criteria:**
  - `assert result.status == 'insufficient_data'`
  - `assert result.is_anomaly is False`
  - No exception raised (test fails naturally if one is)

**Test 2: outlier crosses 3×MAD and logs the numeric value**

- **Input:** 30+ tightly-clustered baseline values + one injected outlier crafted to exceed `3 × MAD`.
- **Processing:** compute median, MAD, threshold = `3 × MAD`, compare incoming value.
- **Expected output:** `AnomalyResult(is_anomaly=True, mad=<float>0, threshold=<float>0, sample_size=n)`.
- **Validation criteria:**
  - `assert result.is_anomaly is True`
  - `assert result.mad > 0`
  - `assert result.threshold == pytest.approx(3 * result.mad)`
  - Confirm the numeric MAD/threshold are present on the returned object (or captured log record) — not just implied by the boolean flag.

---

## Phase 3 — Diagnosis Tests

**Test 1: Tier 1 bypasses the LLM entirely**

- **Input:** `error_code = 'insufficient_funds'` (present in the fixed rule dict).
- **Processing:** `diagnose()` checks the rule-based dict first; should return before ever calling the LLM routing function.
- **Expected output:** `DiagnosisResult(cause='insufficient_funds', tier=1)`.
- **Validation criteria:**
  - `mock_llm_call.assert_not_called()` (patch the LLM function and assert zero invocations)
  - `assert result.tier == 1`

**Test 2: input sanitization strips HTML/SQL before prompt assembly**

- **Input:** raw error string containing HTML tags and SQL-injection-style characters, e.g. `"<script>alert(1)</script>'; DROP TABLE mandates;--"`.
- **Processing:** `sanitize_input()` runs before the prompt is built for the LLM call.
- **Expected output:** sanitized string with all HTML tags and SQL special characters (`< > ' ; --`) removed; the prompt actually sent to the (mocked) LLM matches the sanitized version, never the raw input.
- **Validation criteria:**
  - `assert not any(c in sanitized for c in ['<', '>', ';', '--', "'"])`
  - `assert mock_llm_call.call_args[0][0] == sanitized` (prompt sent equals sanitized text, not raw)

**Test 3: malformed LLM JSON routes to Tier 3, never crashes**

- **Input:** mocked LLM response = invalid JSON string, e.g. `"not valid json {{{"`.
- **Processing:** schema validation (pydantic or equivalent) attempts to parse; catches the failure.
- **Expected output:** `DiagnosisResult(cause=None, tier=3, status='QUARANTINE')`, no exception propagates out of the function.
- **Validation criteria:**
  - `assert result.tier == 3`
  - `assert result.status == 'QUARANTINE'`
  - Test itself fails if any exception escapes `diagnose_tier2()` — assert via `pytest.raises` context that nothing is raised, or simply call it directly and let an uncaught exception fail the test.

---

## Phase 4 — Policy & Idempotency Tests

**Test 1: low-risk event routes to SAFE_HOLD, no alt-rail**

- **Input:** event with high `mandate_reliability_score`, low `amount_at_stake_tier`, distant `days_to_next_billing_cycle` (low urgency).
- **Processing:** weighted scorecard computes `risk_score`; compares against firing threshold.
- **Expected output:** `action == 'SAFE_HOLD'`, alt-rail dispatch function never invoked.
- **Validation criteria:**
  - `assert action == 'SAFE_HOLD'`
  - `mock_alt_rail_dispatch.assert_not_called()`

**Test 2: 50-request concurrent burst → exactly one Alt-Rail execution**

- **Input:** 50 simultaneous async webhook calls, all identical `(mandate_id, billing_cycle)`, high-risk event.
- **Processing:** each call attempts `INSERT ... ON CONFLICT (mandate_id, billing_cycle) DO NOTHING RETURNING *` into `actions_log`; only one insert can succeed by construction.
- **Expected output:** exactly 1 row in `actions_log` for that key; the other 49 calls detect the conflict and return gracefully (no exception, no duplicate dispatch).
- **Validation criteria:**
  - Run via `asyncio.gather(*[dispatch(...) for _ in range(50)])`
  - `assert (await db.fetchval("SELECT COUNT(*) FROM actions_log WHERE mandate_id=$1 AND billing_cycle=$2", ...)) == 1`
  - `assert no unhandled exceptions raised across all 50 tasks`
  - Confirm test runs against a real unique constraint (test DB/transaction), not a mocked lock — this test is only meaningful if it hits the actual constraint.

---

## Phase 5 & 6 — Reconciliation & Metrics Tests

**Test 1: collision inside settlement-hold window auto-refunds**

- **Input:** mandate-path webhook and Alt-Rail confirmation both resolving for the same `idempotency_key`, 4 minutes apart (hold window = 5 minutes).
- **Processing:** reconciliation engine checks the second arrival against the pending hold record before either is marked final on the ledger.
- **Expected output:** one ledger entry marked `settled`, the other marked `auto_refunded` — never two `settled` entries for the same key.
- **Validation criteria:**
  - `assert ledger.count(status='settled', key=idempotency_key) == 1`
  - `assert ledger.count(status='auto_refunded', key=idempotency_key) == 1`

**Test 2: TTL Watchdog escalates a stuck record**

- **Input:** a DB record in `processing` state with `created_at` = now − 10 minutes (use `freezegun` or an injectable clock, don't sleep in-test).
- **Processing:** manually invoke the watchdog sweep function (not the real 60s loop) and check age > 5-minute threshold.
- **Expected output:** record status updated to `manual_escalation`; record appears in the Ops queue view/table.
- **Validation criteria:**
  - `assert record.status == 'manual_escalation'` after the sweep call
  - `assert record.id in [r.id for r in ops_queue.list_pending()]`

---

## What "done" looks like per phase (for the CI prompt's own self-check)

After generating and running each phase's tests, Copilot should confirm three things before moving to the next phase — this is the actual definition of "the layer passed validation," not just "pytest exited 0":

1. All listed assertions pass.
2. No test relies on a real network call (LLM) or real wall-clock sleep — everything is mocked/frozen.
3. The idempotency test (Phase 4) is running against the real unique constraint, not a stub — a green result on a mocked lock proves nothing.

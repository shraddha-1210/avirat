# Role & Architecture Constraints
You are the deterministic developer for Project Avirata (Silent Mandate Death Recovery Agent). You are the ONLY agent permitted to write or edit source files in this workspace.

# Strict Engineering Guardrails
* **No Predictive ML:** Never suggest or implement ML models (Isolation Forest, classifiers, etc.). All decision layers must be purely deterministic and mathematically explainable.
* **Database-Level Idempotency:** Enforce strict idempotency using PostgreSQL `UNIQUE` constraints (`mandate_id`, `billing_cycle`) with `INSERT ... ON CONFLICT DO NOTHING`. Do not rely on application-level locks.
* **LLM Sanitization (Tier 2):** Any raw error string passed to an LLM must be explicitly sanitized (stripped of control chars, capped at 200 chars, escaped) before prompt insertion.
* **Math Gates:** Anomaly detection must use Median Absolute Deviation (MAD) and explicitly check `N >= 30` before processing. Never return a false anomaly on sparse data.
* **Compliance:** Add comments to all alternate-rail execution code stating it is a prototype requiring RBI e-mandate/AFA review.

# Execution Workflow
Always reference `plan.md` for the layer-by-layer specs. Always review `testing_prompt.md` and execute the corresponding `pytest` suite after generating a new layer to ensure no regressions.
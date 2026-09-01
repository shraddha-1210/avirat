"""Central tunables for the Avirata pipeline.

Every weight / threshold / window below is an ILLUSTRATIVE demo value. Anything
touching money movement or risk (risk weights, firing threshold, settlement-hold
window) requires actuary / RBI e-mandate + AFA review before production use.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- external credentials / services ---
    google_api_key: str = ""
    tier2_model: str = "gemini-3.5-flash-lite"
    database_url: str = "postgresql+psycopg://avirata:avirata@localhost:5432/avirata"
    # Sized so the Phase 4 concurrent burst gets one real connection per worker
    # rather than queueing at the pool (which would fake a passing result).
    db_pool_size: int = 120
    db_max_overflow: int = 20

    # --- Layer 1: synthetic dataset (fixed seed => reproducible measurement) ---
    dataset_seed: int = 42
    dataset_n: int = 240

    # --- Layer 2: MAD anomaly detection ---
    mad_threshold: float = 3.0          # flag when |value - median| > mad_threshold * dispersion
    min_sample_size: int = 30           # hard gate; below this -> insufficient_data
    # MAD of a count series is legitimately 0 when >50% of days share one count
    # (e.g. [3,3,3,3,4,5,3...]). A 0 threshold would flag every +1 day as an
    # anomaly, so we floor dispersion at one whole decline event — the natural
    # quantum of count data. Illustrative demo value; retune on real traffic.
    mad_dispersion_floor: float = 1.0

    # --- Layer 3: 3-tier diagnosis ---
    tier2_confidence_threshold: float = 0.85   # below -> Tier 3 quarantine
    llm_input_max_chars: int = 200             # raw error string cap before prompt insertion
    tier2_max_tokens: int = 256                # single small JSON object; no room to ramble

    # --- Layer 4: recovery policy + idempotency ---
    risk_weight_urgency: float = 0.2           # illustrative; require actuary review
    risk_weight_reliability: float = 0.3
    risk_weight_amount: float = 0.4
    risk_weight_cost_benefit: float = 0.1
    risk_firing_threshold: float = 0.6         # alt-rail requires score >= this AND cost-benefit pass
    max_retries: int = 2
    ttl_processing_seconds: int = 300          # a case may sit in 'processing' at most this long
    ttl_watchdog_interval_seconds: int = 60    # demo: cron polling; production would be event-driven

    # --- Layer 5: reconciliation ---
    settlement_hold_seconds: int = 300         # collision window for auto-refund of the losing path


settings = Settings()

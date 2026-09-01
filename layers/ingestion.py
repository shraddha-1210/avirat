"""Layer 1 — Ingestion & synthetic mock data.

Produces a small, deterministic, *real* synthetic decline-event dataset for the
recovery pipeline. Scope is controlled by SIZE (a few hundred events, 4
segments), never by faking: every row is generated from a seeded RNG and a
real cause -> raw-code mapping, and is fully reproducible.

`true_cause` is the HIDDEN ground-truth label. It is generated here but must
never reach Detection or Diagnosis — those layers only ever see
`raw_error_code`. `true_cause` is rejoined at Layer 6 (Measurement) to score
treatment vs. control. Use `to_downstream_payload()` to get the frame that is
safe to hand downstream.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Fixed ontology: the closed set of semantic root causes. Detection/Diagnosis
# classify raw codes *into* this set; they never receive it as input.
# ---------------------------------------------------------------------------
ONTOLOGY_SET: frozenset[str] = frozenset(
    {
        "bank_downtime",
        "insufficient_funds",
        "mandate_revoked",
        "mandate_paused",
        "payer_limit_exceeded",
        "authentication_failure",
        "technical_decline",
        "unknown",
    }
)

# Segments are (bank, mandate_type). Deliberately 4 segments.
BANKS: tuple[str, ...] = ("HDFC", "ICICI", "SBI", "AXIS")
MANDATE_TYPE: str = "UPI_AUTOPAY"

# Real (small) mapping: hidden true_cause -> plausible raw bank decline codes.
# Several causes emit more than one code (ambiguity Tier 2 must resolve);
# `unknown` emits novel strings that must fall through to Tier 3 quarantine.
_CAUSE_TO_RAW_CODES: dict[str, tuple[str, ...]] = {
    "bank_downtime": ("U30", "BANK_NOT_AVAILABLE", "U69"),
    "insufficient_funds": ("U14", "INSUFFICIENT_FUNDS"),
    "mandate_revoked": ("UMN", "MANDATE_REVOKED_BY_PAYER"),
    "mandate_paused": ("M014", "MANDATE_SUSPENDED"),
    "payer_limit_exceeded": ("U16", "PER_TXN_LIMIT_EXCEEDED"),
    "authentication_failure": ("U54", "AUTH_TIMEOUT"),
    "technical_decline": ("U90", "TECHNICAL_ERROR", "U68"),
    "unknown": ("XZ-991", "ERR_UNMAPPED_9007", "gateway declined: reason unclear"),
}

# Relative frequency of each true cause in the generated stream.
_CAUSE_WEIGHTS: dict[str, float] = {
    "bank_downtime": 0.22,
    "insufficient_funds": 0.30,
    "mandate_revoked": 0.10,
    "mandate_paused": 0.08,
    "payer_limit_exceeded": 0.10,
    "authentication_failure": 0.08,
    "technical_decline": 0.09,
    "unknown": 0.03,
}

_AMOUNT_TIERS: tuple[int, ...] = (199, 499, 999, 2999, 4999)

# Fixed clock anchor so event timestamps (and derived billing cycles) are
# reproducible regardless of when the generator runs.
_ANCHOR = datetime(2026, 8, 31, tzinfo=timezone.utc)
_WINDOW_DAYS = 45

DEFAULT_N: int = 240
DEFAULT_SEED: int = 42

# Optional injected incident: a genuine bank-outage burst on one segment on the
# final day. These are REAL events with a real `true_cause` — Detection is never
# told about them and must recover the spike from the counts alone. Off by
# default so the baseline dataset stays byte-identical.
INCIDENT_SEGMENT_BANK: str = "ICICI"
INCIDENT_CAUSE: str = "bank_downtime"
INCIDENT_SIZE: int = 12

# Columns stripped before any frame is handed to Layers 2-4.
DOWNSTREAM_DROP_COLUMNS: tuple[str, ...] = ("true_cause",)

# Public column order of a generated frame.
EVENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "mandate_id",
    "customer_id",
    "bank",
    "mandate_type",
    "segment",
    "event_ts",
    "billing_cycle",
    "amount",
    "mandate_reliability",
    "raw_error_code",
    "true_cause",
)


def generate_events(
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    inject_incident: bool = False,
) -> pd.DataFrame:
    """Return a seeded synthetic decline-event log.

    The frame carries a hidden `true_cause` column drawn from `ONTOLOGY_SET`.
    Identical (n, seed) -> byte-identical frame.

    `inject_incident=True` appends `INCIDENT_SIZE` extra bank-downtime events on
    one segment on the final day, simulating a real bank outage. The returned
    frame then has `n + INCIDENT_SIZE` rows. The incident is ordinary event data
    — Detection receives no marker for it and must recover the spike from the
    daily counts on its own. Incident draws happen after the baseline loop, so
    the first `n` rows are unchanged whether or not this is enabled.
    """
    if n <= 0:
        raise ValueError("n must be positive")

    rng = np.random.default_rng(seed)

    causes = tuple(_CAUSE_WEIGHTS.keys())
    probs = np.array([_CAUSE_WEIGHTS[c] for c in causes], dtype=float)
    probs /= probs.sum()

    # Mandate pool smaller than n so some mandates recur (needed for the
    # reconciliation / idempotency demos downstream).
    n_mandates = max(1, int(n * 0.6))
    mandate_ids = [f"MND-{i:05d}" for i in range(n_mandates)]
    mandate_banks = rng.choice(BANKS, size=n_mandates)
    mandate_reliability = rng.uniform(0.55, 0.98, size=n_mandates).round(3)

    chosen_mandate_idx = rng.integers(0, n_mandates, size=n)
    chosen_causes = rng.choice(len(causes), size=n, p=probs)
    day_offsets = rng.integers(0, _WINDOW_DAYS, size=n)
    second_offsets = rng.integers(0, 86_400, size=n)
    amounts = rng.choice(_AMOUNT_TIERS, size=n)

    rows: list[dict] = []
    for i in range(n):
        m_idx = int(chosen_mandate_idx[i])
        cause = causes[int(chosen_causes[i])]
        raw_codes = _CAUSE_TO_RAW_CODES[cause]
        raw_code = raw_codes[int(rng.integers(0, len(raw_codes)))]
        bank = str(mandate_banks[m_idx])
        event_ts = _ANCHOR - timedelta(
            days=int(day_offsets[i]), seconds=int(second_offsets[i])
        )
        rows.append(
            {
                "event_id": f"EVT-{seed}-{i:05d}",
                "mandate_id": mandate_ids[m_idx],
                "customer_id": f"CUST-{m_idx:05d}",
                "bank": bank,
                "mandate_type": MANDATE_TYPE,
                "segment": f"{bank}:{MANDATE_TYPE}",
                "event_ts": event_ts,
                "billing_cycle": event_ts.strftime("%Y-%m"),
                "amount": int(amounts[i]),
                "mandate_reliability": float(mandate_reliability[m_idx]),
                "raw_error_code": raw_code,
                "true_cause": cause,
            }
        )

    if inject_incident:
        rows.extend(
            _build_incident_rows(
                rng=rng,
                seed=seed,
                start_index=n,
                mandate_ids=mandate_ids,
                mandate_banks=mandate_banks,
                mandate_reliability=mandate_reliability,
            )
        )

    df = pd.DataFrame(rows, columns=list(EVENT_COLUMNS))
    return df


def _build_incident_rows(
    *,
    rng: np.random.Generator,
    seed: int,
    start_index: int,
    mandate_ids: list[str],
    mandate_banks: np.ndarray,
    mandate_reliability: np.ndarray,
) -> list[dict]:
    """Build one day's bank-outage burst on `INCIDENT_SEGMENT_BANK`.

    Real events, real `true_cause`, real raw codes drawn from the same mapping
    as every other row — the only thing special about them is that they cluster
    on one segment on one day, which is what an outage looks like.
    """
    candidates = [i for i, b in enumerate(mandate_banks) if str(b) == INCIDENT_SEGMENT_BANK]
    if not candidates:
        return []

    raw_codes = _CAUSE_TO_RAW_CODES[INCIDENT_CAUSE]
    picks = rng.choice(candidates, size=INCIDENT_SIZE, replace=True)
    amounts = rng.choice(_AMOUNT_TIERS, size=INCIDENT_SIZE)
    seconds = rng.integers(0, 86_400, size=INCIDENT_SIZE)

    rows: list[dict] = []
    for j in range(INCIDENT_SIZE):
        m_idx = int(picks[j])
        event_ts = _ANCHOR.replace(hour=0, minute=0, second=0) + timedelta(
            seconds=int(seconds[j])
        )
        rows.append(
            {
                "event_id": f"EVT-{seed}-{start_index + j:05d}",
                "mandate_id": mandate_ids[m_idx],
                "customer_id": f"CUST-{m_idx:05d}",
                "bank": INCIDENT_SEGMENT_BANK,
                "mandate_type": MANDATE_TYPE,
                "segment": f"{INCIDENT_SEGMENT_BANK}:{MANDATE_TYPE}",
                "event_ts": event_ts,
                "billing_cycle": event_ts.strftime("%Y-%m"),
                "amount": int(amounts[j]),
                "mandate_reliability": float(mandate_reliability[m_idx]),
                "raw_error_code": raw_codes[int(rng.integers(0, len(raw_codes)))],
                "true_cause": INCIDENT_CAUSE,
            }
        )
    return rows


def split_treatment_control(
    df: pd.DataFrame, seed: int = DEFAULT_SEED
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deterministically partition `df` into (treatment, control) by mandate.

    A mandate is assigned wholly to one arm (no straddling). Same seed ->
    same assignment. Row indices from `df` are preserved so callers can
    re-join later. `true_cause` is retained on both frames — it is only
    dropped at the downstream boundary via `to_downstream_payload()`.
    """
    rng = np.random.default_rng(seed)
    mandates = np.array(sorted(df["mandate_id"].unique()))
    shuffled = rng.permutation(mandates)
    cut = len(shuffled) // 2
    treatment_mandates = set(shuffled[:cut].tolist())

    is_treatment = df["mandate_id"].isin(treatment_mandates)
    treatment = df.loc[is_treatment].copy()
    control = df.loc[~is_treatment].copy()
    return treatment, control


def to_downstream_payload(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of `df` with hidden ground-truth columns removed.

    This is the ONLY frame shape that may be handed to Detection / Diagnosis /
    Policy. It still contains `raw_error_code`, which is what those layers act on.
    """
    drop = [c for c in DOWNSTREAM_DROP_COLUMNS if c in df.columns]
    return df.drop(columns=drop).copy()

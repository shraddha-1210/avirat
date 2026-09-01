"""Layer 2 — Detection (MAD anomaly, per (bank, mandate_type) segment).

Deterministic, explainable, no ML. The whole decision is four numbers the model
hands back on every call — median, MAD, threshold, deviation — so any flag can
be re-derived by hand in front of a judge.

Two hard rules from the guardrails:

1. **N >= 30 gate.** Sample size is checked BEFORE any MAD math. A sparse
   segment returns an explicit `insufficient_data` status with `mad=None`. It
   never returns a false anomaly, and it never silently passes — `status` is
   always one of a closed set and is persisted.
2. **Zero-dispersion is handled, not ignored.** MAD of a count series is
   legitimately 0 when more than half the days share one value. A 0 threshold
   would flag every +1 day. We floor the dispersion used for the threshold at
   one whole decline event and report `dispersion_floor_applied=True`, while
   still reporting the raw `mad` honestly.

Note on scaling: we use the *unscaled* MAD (not the 1.4826 robust-sigma
consistency constant), so `threshold` is directly readable as "this many
decline events away from the median". That makes the number defensible in Q&A
rather than needing a normality argument.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable, Literal

import numpy as np
import pandas as pd

from config import settings

# Closed set of terminal statuses — no ambiguous states.
DetectionStatus = Literal["anomaly", "normal", "insufficient_data"]
Direction = Literal["above", "below", "within"]


@dataclass(frozen=True)
class AnomalyResult:
    """Full, auditable output of one anomaly check.

    Every field a judge might ask about is on the object, not just implied by
    the boolean. `threshold` is a DISTANCE from the median, not a level.
    """

    is_anomaly: bool
    status: DetectionStatus
    sample_size: int
    observed_value: float
    median: float | None = None
    mad: float | None = None                    # raw MAD, reported honestly
    dispersion: float | None = None             # max(mad, floor) — what the threshold is built from
    threshold: float | None = None              # mad_threshold * dispersion
    deviation: float | None = None              # |observed - median|
    direction: Direction = "within"
    dispersion_floor_applied: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def check_anomaly(
    segment_history: Iterable[float],
    observed_value: float,
    *,
    k: float | None = None,
    min_sample_size: int | None = None,
    dispersion_floor: float | None = None,
) -> AnomalyResult:
    """Flag `observed_value` against a segment's historical daily decline counts.

    `segment_history` is the series of PRIOR daily decline counts for one
    `(bank, mandate_type)` segment. It must not include `observed_value`.
    """
    k = settings.mad_threshold if k is None else k
    min_sample_size = settings.min_sample_size if min_sample_size is None else min_sample_size
    dispersion_floor = (
        settings.mad_dispersion_floor if dispersion_floor is None else dispersion_floor
    )

    history = np.asarray(list(segment_history), dtype=float)
    history = history[~np.isnan(history)]
    n = int(history.size)
    observed = float(observed_value)

    # --- Gate 1: sample size. Checked before any MAD math. ---
    if n < min_sample_size:
        return AnomalyResult(
            is_anomaly=False,
            status="insufficient_data",
            sample_size=n,
            observed_value=observed,
            reason=(
                f"sample_size={n} < min_sample_size={min_sample_size}; "
                "MAD not computed, no anomaly claimed"
            ),
        )

    median = float(np.median(history))
    mad = float(np.median(np.abs(history - median)))

    # --- Gate 2: zero / near-zero dispersion. ---
    floor_applied = mad < dispersion_floor
    dispersion = float(max(mad, dispersion_floor))

    threshold = float(k * dispersion)
    deviation = float(abs(observed - median))
    is_anomaly = deviation > threshold

    if observed > median:
        direction: Direction = "above"
    elif observed < median:
        direction = "below"
    else:
        direction = "within"

    reason = (
        f"observed={observed:g} median={median:g} deviation={deviation:g} "
        f"{'>' if is_anomaly else '<='} threshold={threshold:g} "
        f"({k:g} x dispersion={dispersion:g}; raw mad={mad:g}"
        + (", dispersion floored" if floor_applied else "")
        + f"); n={n}"
    )

    return AnomalyResult(
        is_anomaly=is_anomaly,
        status="anomaly" if is_anomaly else "normal",
        sample_size=n,
        observed_value=observed,
        median=median,
        mad=mad,
        dispersion=dispersion,
        threshold=threshold,
        deviation=deviation,
        direction=direction,
        dispersion_floor_applied=floor_applied,
        reason=reason,
    )


def build_segment_daily_counts(payload_df: pd.DataFrame) -> dict[str, pd.Series]:
    """Aggregate a decline-event payload into per-segment daily decline counts.

    Days with no declines are real zeros, not missing observations — the series
    is reindexed across the full calendar span of the payload so a quiet day
    counts toward the N>=30 sample and pulls the median down. Dropping them
    would inflate the baseline and suppress genuine spikes.

    `payload_df` must be a downstream payload (no `true_cause`).
    """
    if "true_cause" in payload_df.columns:
        raise ValueError(
            "detection received ground-truth `true_cause`; "
            "pass layers.ingestion.to_downstream_payload(df) instead"
        )

    df = payload_df.copy()
    df["event_date"] = pd.to_datetime(df["event_ts"], utc=True).dt.date

    span = pd.date_range(min(df["event_date"]), max(df["event_date"]), freq="D").date

    out: dict[str, pd.Series] = {}
    for segment, group in df.groupby("segment", sort=True):
        counts = group.groupby("event_date").size()
        out[str(segment)] = counts.reindex(span, fill_value=0).astype(int)
    return out


def run_detection_sweep(
    payload_df: pd.DataFrame, as_of: date | None = None
) -> dict[str, AnomalyResult]:
    """Check every segment's `as_of` day against all strictly-prior days.

    Defaults `as_of` to the last calendar day present in the payload.
    """
    series_by_segment = build_segment_daily_counts(payload_df)
    if not series_by_segment:
        return {}

    if as_of is None:
        as_of = max(max(s.index) for s in series_by_segment.values())

    results: dict[str, AnomalyResult] = {}
    for segment, series in series_by_segment.items():
        history = series[series.index < as_of]
        observed = float(series.get(as_of, 0))
        results[segment] = check_anomaly(history.tolist(), observed)
    return results

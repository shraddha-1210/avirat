"""Phase 2 — Detection tests (from testing.md Phase 2).

Test 1: sample-size gate at N=29.
Test 2: outlier crosses 3xMAD and the numeric MAD/threshold are on the result.
Plus a hand-computed MAD check (plan.md verification table) and the
zero-dispersion guard.
"""
from __future__ import annotations

import pytest

from layers.detection import build_segment_daily_counts, check_anomaly, run_detection_sweep
from layers.ingestion import generate_events, to_downstream_payload


# --- testing.md Phase 2, Test 1: sample-size gate ------------------------------
def test_sample_size_gate_at_n_29_returns_insufficient_data():
    history = [5.0] * 29  # exactly one short of the N>=30 gate
    result = check_anomaly(history, observed_value=500.0)  # wild outlier, must NOT flag

    assert result.status == "insufficient_data"
    assert result.is_anomaly is False
    assert result.sample_size == 29
    assert result.mad is None
    assert result.threshold is None


def test_n_30_passes_the_gate():
    # boundary: N>=30 means 30 is enough
    history = [10.0] * 15 + [12.0] * 15
    result = check_anomaly(history, observed_value=11.0)
    assert result.sample_size == 30
    assert result.status != "insufficient_data"
    assert result.mad is not None


# --- testing.md Phase 2, Test 2: outlier crosses 3xMAD -------------------------
def test_outlier_crosses_three_mad_and_exposes_numeric_values():
    # 30 tightly-clustered baseline values, then an injected outlier.
    history = [10.0] * 15 + [12.0] * 15
    result = check_anomaly(history, observed_value=40.0)

    assert result.is_anomaly is True
    assert result.status == "anomaly"
    assert result.mad > 0
    assert result.threshold == pytest.approx(3 * result.mad)
    assert result.sample_size == 30
    # the numbers are on the object, not merely implied by the boolean
    assert result.median is not None and result.deviation is not None
    assert result.direction == "above"


def test_value_inside_threshold_is_not_an_anomaly():
    history = [10.0] * 15 + [12.0] * 15  # median 11, MAD 1.0, threshold 3.0
    result = check_anomaly(history, observed_value=13.0)  # deviation 2.0 <= 3.0
    assert result.is_anomaly is False
    assert result.status == "normal"


# --- plan.md verification: hand-computed MAD must match the layer --------------
def test_mad_math_matches_hand_computation():
    history = [10.0] * 15 + [12.0] * 15
    # By hand: median = 11.0; |x - 11| is 1.0 for every element; MAD = 1.0.
    result = check_anomaly(history, observed_value=14.5)
    assert result.median == pytest.approx(11.0)
    assert result.mad == pytest.approx(1.0)
    assert result.threshold == pytest.approx(3.0)
    assert result.deviation == pytest.approx(3.5)
    assert result.is_anomaly is True  # 3.5 > 3.0


# --- zero-dispersion guard ----------------------------------------------------
def test_zero_dispersion_does_not_flag_a_one_event_wobble():
    history = [3.0] * 30  # MAD == 0 exactly
    result = check_anomaly(history, observed_value=4.0)
    assert result.mad == pytest.approx(0.0)
    assert result.dispersion_floor_applied is True
    assert result.is_anomaly is False  # a +1 day is not an anomaly
    assert result.status == "normal"


def test_zero_dispersion_still_catches_a_real_spike():
    history = [3.0] * 30
    result = check_anomaly(history, observed_value=50.0)
    assert result.dispersion_floor_applied is True
    assert result.is_anomaly is True


# --- segment aggregation ------------------------------------------------------
def test_daily_counts_keep_zero_declines_as_real_zeros():
    payload = to_downstream_payload(generate_events(n=240, seed=42))
    series_by_segment = build_segment_daily_counts(payload)

    assert set(series_by_segment) == {
        "AXIS:UPI_AUTOPAY",
        "HDFC:UPI_AUTOPAY",
        "ICICI:UPI_AUTOPAY",
        "SBI:UPI_AUTOPAY",
    }
    for series in series_by_segment.values():
        # contiguous calendar span, no gaps
        assert len(series) == len(set(series.index))
        assert (series >= 0).all()
        assert series.sum() > 0
    # every event is accounted for exactly once
    assert sum(int(s.sum()) for s in series_by_segment.values()) == len(payload)


def test_detection_refuses_ground_truth_leakage():
    df_with_truth = generate_events(n=100, seed=42)
    with pytest.raises(ValueError, match="true_cause"):
        build_segment_daily_counts(df_with_truth)


def test_injected_outage_is_recovered_by_the_detector_and_nothing_else_is():
    """End-to-end on real seeded data: the flag is COMPUTED, not scripted.

    The outage is injected into ingestion as ordinary events. Detection is
    handed the downstream payload (no `true_cause`) and must recover exactly
    the affected segment from daily counts alone.
    """
    payload = to_downstream_payload(generate_events(n=240, seed=42, inject_incident=True))
    results = run_detection_sweep(payload)

    flagged = {seg for seg, r in results.items() if r.is_anomaly}
    assert flagged == {"ICICI:UPI_AUTOPAY"}  # the injected segment, and only it

    hit = results["ICICI:UPI_AUTOPAY"]
    assert hit.direction == "above"
    assert hit.deviation > hit.threshold
    assert hit.sample_size >= 30


def test_baseline_dataset_has_no_anomaly_to_find():
    """Without an injected incident nothing is flagged — no phantom anomalies."""
    payload = to_downstream_payload(generate_events(n=240, seed=42))
    results = run_detection_sweep(payload)
    assert not any(r.is_anomaly for r in results.values())


def test_incident_injection_leaves_the_baseline_rows_untouched():
    base = generate_events(n=240, seed=42)
    with_incident = generate_events(n=240, seed=42, inject_incident=True)
    assert len(with_incident) == len(base) + 12
    assert base.equals(with_incident.iloc[:240].reset_index(drop=True))


def test_sweep_returns_a_defined_status_for_every_segment():
    payload = to_downstream_payload(generate_events(n=240, seed=42))
    results = run_detection_sweep(payload)
    assert len(results) == 4
    for segment, result in results.items():
        assert result.status in {"anomaly", "normal", "insufficient_data"}
        # no ambiguous state: a flag always carries its numbers
        if result.status != "insufficient_data":
            assert result.mad is not None and result.threshold is not None

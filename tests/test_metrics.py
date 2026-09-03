"""Layer 6 — metrics and the audit trace, against real Postgres.

plan.md's verification row for this layer is "Honest metrics, no fudging", so
most of these tests are about what the metrics must REFUSE to claim: no zeros
standing in for unmeasured values, no in-flight work counted as instant, no
uncontrolled subtraction presented as a lift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from layers.metrics import (
    compute_metrics,
    decision_trace,
    mttr_by_tier,
    recovered_rupees,
    recovery_rate,
)
from layers.reconciliation import open_hold, resolve_path
from models import ActionsLog, DeclineEvent, Diagnosis, Mandate, QuarantineQueue

CYCLE = "2026-09"
BASE_TS = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _mandate(session, mandate_id, *, bank="ICICI"):
    session.add(
        Mandate(
            mandate_id=mandate_id,
            customer_id=f"CUST-{mandate_id[-5:]}",
            bank=bank,
            mandate_type="UPI_AUTOPAY",
            reliability_score=0.9,
        )
    )
    session.flush()      # decline_events FKs onto mandates


def _event(session, event_id, mandate_id, *, amount=1000, arm=None, code="U30"):
    session.add(
        DeclineEvent(
            event_id=event_id,
            mandate_id=mandate_id,
            billing_cycle=CYCLE,
            segment="ICICI:UPI_AUTOPAY",
            bank="ICICI",
            mandate_type="UPI_AUTOPAY",
            event_ts=BASE_TS,
            amount=amount,
            raw_error_code=code,
            arm=arm,
        )
    )
    session.flush()      # diagnoses FKs onto decline_events


def _diagnosis(session, event_id, *, tier=1, cause="bank_downtime"):
    session.add(
        Diagnosis(
            event_id=event_id,
            tier=tier,
            cause=cause,
            confidence=1.0,
            status="resolved" if cause else "QUARANTINE",
            raw_input="U30",
        )
    )


def _action(session, mandate_id, *, created, resolved=None, action_type="RETRY"):
    row = ActionsLog(
        mandate_id=mandate_id,
        billing_cycle=CYCLE,
        action_type=action_type,
        params={},
        status="settled" if resolved else "processing",
    )
    session.add(row)
    session.flush()
    row.created_at = created
    row.resolved_at = resolved
    session.flush()
    return row


# ---------------------------------------------------------------------------
# MTTR
# ---------------------------------------------------------------------------
def test_mttr_measures_dispatch_to_resolution_per_tier(pg_session):
    _mandate(pg_session, "MND-00001")
    _event(pg_session, "EVT-1", "MND-00001")
    _diagnosis(pg_session, "EVT-1", tier=1)
    _action(pg_session, "MND-00001", created=BASE_TS, resolved=BASE_TS + timedelta(hours=2))
    pg_session.commit()

    tiers = {t.tier: t for t in mttr_by_tier(pg_session)}
    assert tiers[1].mttr_seconds == 7200.0
    assert tiers[1].resolved_count == 1
    assert tiers[1].in_flight_count == 0


def test_in_flight_actions_are_excluded_not_counted_as_zero(pg_session):
    """An unresolved action has taken an UNKNOWN time, not zero.

    Averaging it in as 0 would make MTTR fall as the backlog grows — exactly
    backwards, and the kind of number that looks like an improvement.
    """
    _mandate(pg_session, "MND-00001")
    _mandate(pg_session, "MND-00002")
    _event(pg_session, "EVT-1", "MND-00001")
    _event(pg_session, "EVT-2", "MND-00002")
    _diagnosis(pg_session, "EVT-1", tier=1)
    _diagnosis(pg_session, "EVT-2", tier=1)
    _action(pg_session, "MND-00001", created=BASE_TS, resolved=BASE_TS + timedelta(hours=4))
    _action(pg_session, "MND-00002", created=BASE_TS, resolved=None)   # still running
    pg_session.commit()

    tier1 = {t.tier: t for t in mttr_by_tier(pg_session)}[1]
    assert tier1.mttr_seconds == 14400.0, "the in-flight action skewed the mean"
    assert tier1.resolved_count == 1
    assert tier1.in_flight_count == 1


def test_mttr_is_none_when_nothing_has_resolved(pg_session):
    """No data must read as 'not measured', never as an instant recovery."""
    _mandate(pg_session, "MND-00001")
    _event(pg_session, "EVT-1", "MND-00001")
    _diagnosis(pg_session, "EVT-1", tier=2)
    _action(pg_session, "MND-00001", created=BASE_TS, resolved=None)
    pg_session.commit()

    tier2 = {t.tier: t for t in mttr_by_tier(pg_session)}[2]
    assert tier2.mttr_seconds is None
    assert "not measurable" in tier2.note


# ---------------------------------------------------------------------------
# Rupees Recovered — the "no fudging" metric
# ---------------------------------------------------------------------------
def test_recovered_is_exactly_treatment_minus_control(pg_session):
    """plan.md verification: the delta must be the literal subtraction."""
    for i, (arm, amount) in enumerate(
        [("treatment", 5000), ("treatment", 3000), ("control", 2000)], start=1
    ):
        mid = f"MND-{i:05d}"
        _mandate(pg_session, mid)
        _event(pg_session, f"EVT-{i}", mid, amount=amount, arm=arm)
        pg_session.flush()
        open_hold(pg_session, mandate_id=mid, billing_cycle=CYCLE,
                  path="mandate", amount=amount)
        resolve_path(pg_session, mandate_id=mid, billing_cycle=CYCLE, path="mandate")
    pg_session.commit()

    r = recovered_rupees(pg_session)
    assert r.treatment_recovered == 8000
    assert r.control_recovered == 2000
    assert r.delta == 8000 - 2000 == 6000


def test_unsettled_holds_do_not_count_as_recovered(pg_session):
    """Only money actually kept counts. A pending hold is not revenue."""
    _mandate(pg_session, "MND-00001")
    _event(pg_session, "EVT-1", "MND-00001", amount=5000, arm="treatment")
    pg_session.flush()
    open_hold(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE,
              path="alt_rail", amount=5000)
    pg_session.commit()

    assert recovered_rupees(pg_session).treatment_recovered == 0


def test_refunded_money_does_not_count_as_recovered(pg_session):
    """An auto-refunded collision was returned to the customer.

    Counting both sides of a collision would report a double-charge as double
    revenue — the exact failure Layer 5 exists to prevent.
    """
    _mandate(pg_session, "MND-00001")
    _event(pg_session, "EVT-1", "MND-00001", amount=5000, arm="treatment")
    pg_session.flush()
    open_hold(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE,
              path="alt_rail", amount=5000)
    open_hold(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE,
              path="mandate", amount=5000)
    resolve_path(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE, path="alt_rail")
    resolve_path(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE, path="mandate")
    pg_session.commit()

    assert recovered_rupees(pg_session).treatment_recovered == 5000


def test_missing_arm_assignment_reports_not_computable(pg_session):
    """Zero would be indistinguishable from 'the intervention did nothing'."""
    _mandate(pg_session, "MND-00001")
    _event(pg_session, "EVT-1", "MND-00001", amount=5000, arm=None)
    pg_session.flush()
    open_hold(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE,
              path="mandate", amount=5000)
    resolve_path(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE, path="mandate")
    pg_session.commit()

    r = recovered_rupees(pg_session)
    assert r.computable is False
    assert r.per_mandate_delta is None
    assert "arm assignment" in r.note


def test_unequal_arms_are_flagged_and_normalised(pg_session):
    """A raw delta across unequal arms is not a controlled comparison."""
    for i, arm in enumerate(["treatment", "treatment", "treatment", "control"], start=1):
        mid = f"MND-{i:05d}"
        _mandate(pg_session, mid)
        _event(pg_session, f"EVT-{i}", mid, amount=1000, arm=arm)
        pg_session.flush()
        open_hold(pg_session, mandate_id=mid, billing_cycle=CYCLE,
                  path="mandate", amount=1000)
        resolve_path(pg_session, mandate_id=mid, billing_cycle=CYCLE, path="mandate")
    pg_session.commit()

    r = recovered_rupees(pg_session)
    assert r.treatment_mandates == 3 and r.control_mandates == 1
    assert r.delta == 2000
    # 3000/3 - 1000/1 == 0: the raw delta says +2000, the fair figure says 0.
    assert r.per_mandate_delta == 0.0
    assert "per_mandate_delta" in r.note


# ---------------------------------------------------------------------------
# recovery rate
# ---------------------------------------------------------------------------
def test_recovery_rate_denominator_includes_in_flight_actions(pg_session):
    """Dividing by resolved-only would flatter the number as the backlog grows."""
    _mandate(pg_session, "MND-00001")
    _mandate(pg_session, "MND-00002")
    _action(pg_session, "MND-00001", created=BASE_TS, resolved=BASE_TS + timedelta(hours=1))
    _action(pg_session, "MND-00002", created=BASE_TS, resolved=None)
    pg_session.commit()

    r = recovery_rate(pg_session)
    assert r["actions_total"] == 2
    assert r["actions_in_flight"] == 1
    assert r["rate"] == 0.5


def test_resolution_beyond_the_sla_does_not_count(pg_session):
    _mandate(pg_session, "MND-00001")
    _action(pg_session, "MND-00001", created=BASE_TS, resolved=BASE_TS + timedelta(hours=30))
    pg_session.commit()

    r = recovery_rate(pg_session, sla_hours=24)
    assert r["actions_resolved"] == 1
    assert r["resolved_within_sla"] == 0
    assert r["rate"] == 0.0


def test_metrics_on_an_empty_database_do_not_invent_numbers(pg_session):
    m = compute_metrics(pg_session)
    assert m.recovery_rate["rate"] is None
    assert m.recovered["computable"] is False
    assert m.mttr_by_tier == []
    assert "Controlled simulation" in m.disclaimer


# ---------------------------------------------------------------------------
# audit trace
# ---------------------------------------------------------------------------
def test_decision_trace_returns_the_whole_journey(pg_session):
    _mandate(pg_session, "MND-00001")
    _event(pg_session, "EVT-1", "MND-00001", amount=4000, arm="treatment")
    _diagnosis(pg_session, "EVT-1", tier=2, cause="mandate_revoked")
    _action(pg_session, "MND-00001", created=BASE_TS,
            resolved=BASE_TS + timedelta(hours=1), action_type="ALT_RAIL")
    pg_session.flush()
    open_hold(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE,
              path="alt_rail", amount=4000)
    resolve_path(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE, path="alt_rail")
    pg_session.commit()

    trace = decision_trace(pg_session, mandate_id="MND-00001", billing_cycle=CYCLE)
    assert trace["found"] is True
    assert trace["events"][0]["raw_error_code"] == "U30"
    assert trace["diagnoses"][0]["tier"] == 2
    assert trace["diagnoses"][0]["cause"] == "mandate_revoked"
    assert trace["action"]["action_type"] == "ALT_RAIL"
    assert trace["reconciliation"][0]["status"] == "settled"


def test_decision_trace_for_an_unknown_key_is_an_answer_not_an_error(pg_session):
    trace = decision_trace(pg_session, mandate_id="MND-NOPE", billing_cycle="1999-01")
    assert trace["found"] is False
    assert trace["events"] == [] and trace["action"] is None


def test_quarantine_backlog_surfaces_sample_cases(pg_session):
    for i in range(3):
        pg_session.add(
            QuarantineQueue(
                event_id=f"EVT-{i}",
                raw_input=f"XZ-99{i}",
                tier_attempted=3,
                reason="llm could not map the string",
            )
        )
    pg_session.commit()

    q = compute_metrics(pg_session).quarantine
    assert q["total"] == 3 and q["pending_ops_review"] == 3
    assert len(q["samples"]) == 3
    assert all("raw_input" in s for s in q["samples"])

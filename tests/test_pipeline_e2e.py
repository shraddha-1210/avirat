"""Phase 4 end-to-end: diagnosis -> policy -> idempotent dispatch -> comms.

Real Postgres (so the idempotency guard is the actual constraint), mocked LLM
(so no test touches the network — `conftest._no_real_api_calls` enforces that).
These are the plan.md Phase 7 scenarios, run early because the cascade they
describe is exactly what Phase 4 assembles.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from layers.pipeline import run_recovery
from models import ActionsLog, CommunicationState, DeclineEvent, Diagnosis, Mandate, QuarantineQueue

MANDATE_ID = "MND-00042"
CYCLE = "2026-09"


@pytest.fixture
def seeded(pg_session):
    """Ingest the mandate and its decline events first, as production does.

    `diagnoses.event_id` is a real FK onto `decline_events`, so recovery cannot
    run for an event that was never ingested — the schema enforces the ordering
    rather than trusting callers to respect it.
    """
    pg_session.add(
        Mandate(
            mandate_id=MANDATE_ID,
            customer_id="CUST-00042",
            bank="ICICI",
            mandate_type="UPI_AUTOPAY",
            reliability_score=0.9,
        )
    )
    for event_id in ("EVT-1", "EVT-1-REPLAY"):
        pg_session.add(
            DeclineEvent(
                event_id=event_id,
                mandate_id=MANDATE_ID,
                billing_cycle=CYCLE,
                segment="ICICI:UPI_AUTOPAY",
                bank="ICICI",
                mandate_type="UPI_AUTOPAY",
                event_ts=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
                amount=1200,
                raw_error_code="U30",
            )
        )
    pg_session.commit()
    return pg_session


def _run(session, **overrides):
    kwargs = dict(
        event_id="EVT-1",
        mandate_id=MANDATE_ID,
        billing_cycle=CYCLE,
        raw_error_code="U30",       # Tier 1: bank_downtime
        amount=1200,
        mandate_reliability=0.9,
        days_to_next_cycle=15,
        use_cache=False,
    )
    kwargs.update(overrides)
    return run_recovery(session, **kwargs)


def test_happy_path_tier1_downtime_retries_without_alt_rail(seeded, pg_session, mock_llm_call):
    """Scenario 1: a known code resolves at Tier 1, never reaching the LLM."""
    outcome = _run(pg_session)
    pg_session.commit()

    assert outcome.diagnosis["tier"] == 1
    assert outcome.diagnosis["cause"] == "bank_downtime"
    assert outcome.decision["action"] == "RETRY"
    assert outcome.fired is True
    mock_llm_call.assert_not_called()

    row = pg_session.execute(select(ActionsLog)).scalar_one()
    assert row.action_type == "RETRY"


def test_tier2_insufficient_funds_nudges_and_records_the_message(seeded, pg_session, mock_llm_call):
    """Scenario 2: an ambiguous string resolves at Tier 2 and triggers a nudge."""
    outcome = _run(pg_session, raw_error_code="LOW_BAL_AT_DEBIT_TIME")
    pg_session.commit()

    assert outcome.diagnosis["tier"] == 2
    assert outcome.diagnosis["cause"] == "insufficient_funds"
    assert outcome.decision["action"] == "NUDGE_BALANCE"
    assert outcome.comms["sent"] is True
    mock_llm_call.assert_called_once()

    state = pg_session.execute(select(CommunicationState)).scalar_one()
    assert state.reminder_count == 1
    assert state.alt_rail_live is False


def test_high_risk_revoked_mandate_fires_alt_rail_and_claims_the_mutex(
    seeded, pg_session, mock_llm_call
):
    """Scenario 3: alt-rail fires AND silences the standard reminder job."""
    mock_llm_call.return_value = json.dumps(
        {"cause": "mandate_revoked", "confidence": 0.97, "rationale": "payer revoked"}
    )
    outcome = _run(
        pg_session,
        raw_error_code="MANDATE_REVOKED_BY_PAYER",
        amount=4800,
        mandate_reliability=0.55,
        days_to_next_cycle=1,
    )
    pg_session.commit()

    assert outcome.decision["action"] == "ALT_RAIL"
    assert outcome.decision["risk"]["cost_benefit_passed"] is True
    assert outcome.comms == {"mutex": "alt_rail_live", "claimed": True}

    state = pg_session.execute(select(CommunicationState)).scalar_one()
    assert state.alt_rail_live is True


def test_unmappable_code_quarantines_and_fires_no_money_action(seeded, pg_session, mock_llm_call):
    """Scenario 4: a novel string reaches Ops, not the payment rail."""
    mock_llm_call.return_value = json.dumps(
        {"cause": "unknown", "confidence": 0.1, "rationale": "no match"}
    )
    outcome = _run(pg_session, raw_error_code="XZ-991")
    pg_session.commit()

    assert outcome.quarantined is True
    assert outcome.diagnosis["tier"] == 3
    assert outcome.decision["action"] == "MANUAL_REVIEW"

    quarantine = pg_session.execute(select(QuarantineQueue)).scalar_one()
    assert quarantine.event_id == "EVT-1"
    assert quarantine.status == "pending_ops_review"

    # MANUAL_REVIEW is still recorded — it holds the idempotency key so a replay
    # cannot later fire a real action on the same failure.
    assert pg_session.execute(select(ActionsLog)).scalar_one().action_type == "MANUAL_REVIEW"
    assert pg_session.execute(
        select(func.count()).select_from(CommunicationState)
    ).scalar_one() == 0, "a quarantined event must not message the customer"


def test_replayed_event_neither_refires_nor_re_messages(seeded, pg_session, mock_llm_call):
    """A webhook retry must not produce a second nudge.

    The idempotency guard protects money AND comms: without the early return,
    the customer gets a duplicate message even though only one action exists.
    """
    first = _run(pg_session, raw_error_code="LOW_BAL_AT_DEBIT_TIME")
    pg_session.commit()
    second = _run(pg_session, event_id="EVT-1-REPLAY", raw_error_code="LOW_BAL_AT_DEBIT_TIME")
    pg_session.commit()

    assert first.fired is True and first.comms["sent"] is True
    assert second.fired is False
    assert second.comms is None
    assert "idempotent guard" in second.reason

    assert pg_session.execute(select(func.count()).select_from(ActionsLog)).scalar_one() == 1
    state = pg_session.execute(select(CommunicationState)).scalar_one()
    assert state.reminder_count == 1, "the replay sent a second message"


def test_every_event_is_diagnosed_and_recorded_even_when_not_fired(seeded, pg_session, mock_llm_call):
    """A replay still leaves a diagnosis row: the event was seen and explained."""
    _run(pg_session)
    pg_session.commit()
    _run(pg_session, event_id="EVT-1-REPLAY")
    pg_session.commit()

    assert pg_session.execute(select(func.count()).select_from(Diagnosis)).scalar_one() == 2
    assert pg_session.execute(select(func.count()).select_from(ActionsLog)).scalar_one() == 1

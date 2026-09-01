"""Layer 4d/4e — comms mutex and TTL watchdog, against real Postgres.

Both are stateful in the database (the mutex is a row, not a process flag), so
both take the `pg_session` fixture rather than being mocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from config import settings
from layers.comms_orchestrator import (
    clear_alt_rail_live,
    is_alt_rail_live,
    send_nudge,
    send_standard_reminder,
    set_alt_rail_live,
)
from layers.recovery_policy import decide, fire_action_idempotent
from models import ActionsLog, CommunicationState, OpsEscalationQueue
from tasks.ttl_watchdog import sweep_stuck_actions

MANDATE_ID = "MND-00042"
CYCLE = "2026-09"


# ---------------------------------------------------------------------------
# 4d — comms mutex
# ---------------------------------------------------------------------------
def test_standard_reminder_sends_when_alt_rail_is_not_live(pg_session):
    result = send_standard_reminder(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE)
    pg_session.commit()
    assert result.sent is True
    assert result.suppressed_by is None
    assert result.reminder_count == 1


def test_alt_rail_live_suppresses_the_standard_reminder(pg_session):
    """THE failure this layer exists to prevent: a "we'll retry" message going
    out while the customer is holding an alt-rail payment link."""
    set_alt_rail_live(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE)
    pg_session.commit()

    result = send_standard_reminder(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE)
    pg_session.commit()

    assert result.sent is False
    assert result.suppressed_by == "alt_rail_live"
    assert result.reminder_count == 0, "a suppressed message must not count as sent"


def test_nudge_is_still_permitted_while_alt_rail_is_live(pg_session):
    """A balance nudge and an alt-rail link both ask for the same rupee — they
    are not contradictory, so the mutex must not over-suppress."""
    set_alt_rail_live(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE)
    result = send_nudge(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE)
    pg_session.commit()
    assert result.sent is True


def test_mutex_is_scoped_to_one_billing_cycle(pg_session):
    """Suppressing this month must not silence next month."""
    set_alt_rail_live(pg_session, mandate_id=MANDATE_ID, billing_cycle="2026-09")
    pg_session.commit()

    blocked = send_standard_reminder(pg_session, mandate_id=MANDATE_ID, billing_cycle="2026-09")
    allowed = send_standard_reminder(pg_session, mandate_id=MANDATE_ID, billing_cycle="2026-10")
    pg_session.commit()

    assert blocked.sent is False
    assert allowed.sent is True


def test_clearing_the_mutex_restores_normal_messaging(pg_session):
    set_alt_rail_live(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE)
    assert is_alt_rail_live(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE) is True

    clear_alt_rail_live(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE)
    pg_session.commit()

    assert is_alt_rail_live(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE) is False
    assert send_standard_reminder(
        pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE
    ).sent is True


def test_comms_state_row_is_created_once_not_duplicated(pg_session):
    """The get-or-create must not accumulate rows on repeated sends."""
    for _ in range(5):
        send_standard_reminder(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE)
    pg_session.commit()

    rows = pg_session.execute(
        select(func.count()).select_from(CommunicationState)
        .where(CommunicationState.mandate_id == MANDATE_ID)
        .where(CommunicationState.billing_cycle == CYCLE)
    ).scalar_one()
    assert rows == 1


# ---------------------------------------------------------------------------
# 4e — TTL watchdog
# ---------------------------------------------------------------------------
def _stuck_action(
    pg_session,
    *,
    age_seconds: int,
    cycle: str = CYCLE,
    status: str = "processing",
    now: datetime | None = None,
):
    """Insert an action whose created_at is deliberately backdated.

    `now` is the anchor the backdating is measured from. Boundary tests must
    pass the SAME anchor to `sweep_stuck_actions`, or the few milliseconds
    between the two calls silently shift the row across the cutoff.
    """
    decision = decide(
        cause="mandate_revoked",
        diagnosis_tier=2,
        diagnosis_status="resolved",
        days_to_next_cycle=1,
        mandate_reliability=0.55,
        amount=4800,
    )
    fire_action_idempotent(
        pg_session, mandate_id=MANDATE_ID, billing_cycle=cycle, decision=decision
    )
    pg_session.flush()
    row = pg_session.execute(
        select(ActionsLog).where(ActionsLog.billing_cycle == cycle)
    ).scalar_one()
    row.created_at = (now or datetime.now(timezone.utc)) - timedelta(seconds=age_seconds)
    row.status = status
    pg_session.commit()
    return row


def test_action_stuck_past_the_ttl_is_escalated(pg_session):
    """testing.md/plan.md: stuck 'processing' > 5 min -> auto-escalated to Ops."""
    _stuck_action(pg_session, age_seconds=settings.ttl_processing_seconds + 60)

    result = sweep_stuck_actions(pg_session)
    pg_session.commit()

    assert result.escalated == 1
    assert (MANDATE_ID, CYCLE) in result.escalated_keys

    row = pg_session.execute(select(ActionsLog)).scalar_one()
    assert row.status == "manual_escalation"

    escalation = pg_session.execute(select(OpsEscalationQueue)).scalar_one()
    assert escalation.mandate_id == MANDATE_ID
    assert escalation.reason == "ttl_exceeded_processing"
    assert escalation.source_layer == "ttl_watchdog"
    assert escalation.status == "open"


def test_fresh_action_inside_the_ttl_is_left_alone(pg_session):
    """A recovery in flight for 10 seconds is working, not stuck."""
    _stuck_action(pg_session, age_seconds=10)

    result = sweep_stuck_actions(pg_session)
    pg_session.commit()

    assert result.escalated == 0
    assert pg_session.execute(select(ActionsLog)).scalar_one().status == "processing"
    assert pg_session.execute(select(func.count()).select_from(OpsEscalationQueue)).scalar_one() == 0


def test_action_exactly_at_the_boundary_is_not_escalated(pg_session):
    """The comparison is strictly older-than, so a row exactly at the TTL is safe.

    Pinning this stops the boundary drifting silently on a future refactor.
    """
    now = datetime.now(timezone.utc)
    _stuck_action(pg_session, age_seconds=settings.ttl_processing_seconds, now=now)

    result = sweep_stuck_actions(pg_session, now=now)
    pg_session.commit()
    assert result.escalated == 0


def test_sweep_is_idempotent_and_does_not_re_escalate(pg_session):
    """A 60s poller runs constantly; double-escalating would spam the Ops queue."""
    _stuck_action(pg_session, age_seconds=settings.ttl_processing_seconds + 60)

    first = sweep_stuck_actions(pg_session)
    pg_session.commit()
    second = sweep_stuck_actions(pg_session)
    pg_session.commit()

    assert first.escalated == 1
    assert second.escalated == 0
    assert pg_session.execute(select(func.count()).select_from(OpsEscalationQueue)).scalar_one() == 1


def test_terminal_actions_are_never_escalated(pg_session):
    """Only in-flight work can be stuck. A settled row stays settled however old."""
    _stuck_action(
        pg_session, age_seconds=settings.ttl_processing_seconds * 10, status="settled"
    )

    result = sweep_stuck_actions(pg_session)
    pg_session.commit()

    assert result.escalated == 0
    assert pg_session.execute(select(ActionsLog)).scalar_one().status == "settled"


def test_sweep_reports_the_cutoff_it_used(pg_session):
    """The audit trail needs the cutoff, not just the count."""
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    result = sweep_stuck_actions(pg_session, now=now)
    assert result.ttl_seconds == settings.ttl_processing_seconds
    assert result.cutoff == now - timedelta(seconds=settings.ttl_processing_seconds)
    assert "cutoff" in result.to_dict()

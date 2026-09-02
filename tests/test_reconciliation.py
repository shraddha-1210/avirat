"""Layer 5 — reconciliation, against real Postgres.

The central guarantee — at most one `settled` row per (mandate_id,
billing_cycle) — is a partial unique index, so like Layer 4c these tests are
only meaningful against the real database. `pg_session` skips loudly rather
than substituting another dialect.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from config import settings
from db import get_session
from layers.reconciliation import (
    AUTO_REFUNDED,
    CLOSED_SUPERSEDED,
    EXPIRED_ESCALATED,
    PENDING,
    SETTLED,
    open_hold,
    resolve_path,
    sweep_expired_holds,
)
from models import OpsEscalationQueue, ReconciliationLedger

MANDATE_ID = "MND-00042"
CYCLE = "2026-09"
AMOUNT = 2999


def _open_both(session, *, now=None):
    open_hold(session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
              path="alt_rail", amount=AMOUNT, now=now)
    open_hold(session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
              path="mandate", amount=AMOUNT, now=now)
    session.commit()


def _count(session, status):
    return session.execute(
        select(func.count()).select_from(ReconciliationLedger)
        .where(ReconciliationLedger.mandate_id == MANDATE_ID)
        .where(ReconciliationLedger.billing_cycle == CYCLE)
        .where(ReconciliationLedger.status == status)
    ).scalar_one()


def test_partial_unique_index_exists(pg_session):
    """Guard on the guard: the exactly-one-settled rule is an INDEX, not code.

    If a schema change dropped it, the collision tests would still pass under
    low concurrency while the system was unsafe under load.
    """
    assert pg_session.bind.dialect.name == "postgresql"
    definition = pg_session.execute(
        text("select indexdef from pg_indexes where indexname = 'uq_recon_single_settled'")
    ).scalar_one_or_none()
    assert definition is not None, "uq_recon_single_settled is missing from the live schema"
    assert "UNIQUE" in definition
    assert "WHERE" in definition and "settled" in definition


def test_open_hold_is_idempotent(pg_session):
    """A retried dispatch must not reset the hold clock and dodge expiry."""
    first = open_hold(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
                      path="alt_rail", amount=AMOUNT)
    pg_session.commit()
    opened_at = pg_session.execute(select(ReconciliationLedger.opened_at)).scalar_one()

    second = open_hold(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
                       path="alt_rail", amount=AMOUNT)
    pg_session.commit()

    assert first is True and second is False
    assert pg_session.execute(select(func.count()).select_from(ReconciliationLedger)).scalar_one() == 1
    assert pg_session.execute(select(ReconciliationLedger.opened_at)).scalar_one() == opened_at


def test_single_path_settles_cleanly(pg_session):
    _open_both(pg_session)
    result = resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE, path="alt_rail")
    pg_session.commit()

    assert result.status == SETTLED
    assert result.collided_with is None
    assert _count(pg_session, SETTLED) == 1
    assert _count(pg_session, PENDING) == 1


def test_collision_inside_hold_window_auto_refunds(pg_session):
    """testing.md Phase 5 Test 1: both paths resolve 4 min apart, window 5 min.

    Exactly one `settled` and one `auto_refunded` — never two settled.
    """
    opened = datetime.now(timezone.utc) - timedelta(minutes=4)
    _open_both(pg_session, now=opened)

    first = resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
                         path="mandate", now=opened)
    pg_session.commit()
    second = resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
                          path="alt_rail", now=opened + timedelta(minutes=4))
    pg_session.commit()

    assert first.status == SETTLED
    assert second.status == AUTO_REFUNDED
    assert second.collided_with == "mandate"
    assert second.within_hold_window is True
    assert second.refunded_amount == AMOUNT

    assert _count(pg_session, SETTLED) == 1
    assert _count(pg_session, AUTO_REFUNDED) == 1
    # An in-window collision is the designed path — it must not page a human.
    assert pg_session.execute(
        select(func.count()).select_from(OpsEscalationQueue)
    ).scalar_one() == 0


def test_late_collision_refunds_and_escalates(pg_session):
    """Outside the window the refund still happens, but a human is told.

    The hold had already expired, so an operator may have acted on this key —
    silently refunding would hide that from them.
    """
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    _open_both(pg_session, now=opened)

    resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
                 path="mandate", now=opened)
    pg_session.commit()
    late = resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
                        path="alt_rail", now=opened + timedelta(minutes=20))
    pg_session.commit()

    assert late.status == AUTO_REFUNDED
    assert late.within_hold_window is False
    escalation = pg_session.execute(select(OpsEscalationQueue)).scalar_one()
    assert escalation.reason == "late_collision_after_hold"
    assert escalation.source_layer == "reconciliation"


def test_replayed_settlement_webhook_is_ignored(pg_session):
    """A duplicate webhook must not flip a settled row into a refund."""
    _open_both(pg_session)
    resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE, path="alt_rail")
    pg_session.commit()
    replay = resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE, path="alt_rail")
    pg_session.commit()

    assert replay.status == SETTLED
    assert "replay ignored" in replay.reason
    assert _count(pg_session, SETTLED) == 1
    assert _count(pg_session, AUTO_REFUNDED) == 0


def test_webhook_for_unattempted_path_still_records_money(pg_session):
    """A settlement for a path we never dispatched must not be dropped silently."""
    open_hold(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
              path="alt_rail", amount=AMOUNT)
    pg_session.commit()

    result = resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
                          path="mandate", amount=AMOUNT)
    pg_session.commit()

    assert result.status == SETTLED
    assert pg_session.execute(
        select(func.count()).select_from(ReconciliationLedger)
    ).scalar_one() == 2


# ---------------------------------------------------------------------------
# expiry sweep
# ---------------------------------------------------------------------------
def test_expired_hold_with_nothing_settled_escalates(pg_session):
    """plan.md item 16: neither path resolved -> expired_escalated + Ops."""
    opened = datetime.now(timezone.utc) - timedelta(seconds=settings.settlement_hold_seconds + 60)
    _open_both(pg_session, now=opened)

    result = sweep_expired_holds(pg_session)
    pg_session.commit()

    assert result.escalated == 2
    assert result.superseded == 0
    assert _count(pg_session, EXPIRED_ESCALATED) == 2
    assert pg_session.execute(
        select(func.count()).select_from(OpsEscalationQueue)
    ).scalar_one() == 2


def test_fresh_hold_inside_window_is_untouched(pg_session):
    _open_both(pg_session)
    result = sweep_expired_holds(pg_session)
    pg_session.commit()

    assert result.scanned == 0 and result.escalated == 0
    assert _count(pg_session, PENDING) == 2


def test_hold_exactly_at_the_window_boundary_is_not_swept(pg_session):
    """Strictly older-than, pinned so the boundary cannot drift silently."""
    now = datetime.now(timezone.utc)
    _open_both(pg_session, now=now - timedelta(seconds=settings.settlement_hold_seconds))

    result = sweep_expired_holds(pg_session, now=now)
    pg_session.commit()
    assert result.escalated == 0


def test_sibling_of_a_settled_path_is_closed_not_escalated(pg_session):
    """The alt rail never collected — there is nothing to refund or escalate.

    Escalating it would bury the genuinely ambiguous cases in noise.
    """
    opened = datetime.now(timezone.utc) - timedelta(seconds=settings.settlement_hold_seconds + 60)
    _open_both(pg_session, now=opened)
    resolve_path(pg_session, mandate_id=MANDATE_ID, billing_cycle=CYCLE,
                 path="mandate", now=opened)
    pg_session.commit()

    result = sweep_expired_holds(pg_session)
    pg_session.commit()

    assert result.superseded == 1
    assert result.escalated == 0
    assert _count(pg_session, SETTLED) == 1
    assert _count(pg_session, CLOSED_SUPERSEDED) == 1
    assert pg_session.execute(
        select(func.count()).select_from(OpsEscalationQueue)
    ).scalar_one() == 0


def test_sweep_is_idempotent(pg_session):
    """The 5-min cron runs forever; double-escalating would spam Ops."""
    opened = datetime.now(timezone.utc) - timedelta(seconds=settings.settlement_hold_seconds + 60)
    _open_both(pg_session, now=opened)

    first = sweep_expired_holds(pg_session)
    pg_session.commit()
    second = sweep_expired_holds(pg_session)
    pg_session.commit()

    assert first.escalated == 2
    assert second.escalated == 0 and second.scanned == 0
    assert pg_session.execute(
        select(func.count()).select_from(OpsEscalationQueue)
    ).scalar_one() == 2


# ---------------------------------------------------------------------------
# concurrency — the reason the guarantee is an index and not an if-statement
# ---------------------------------------------------------------------------
def test_concurrent_settlements_yield_exactly_one_settled(pg_session):
    """Both webhooks land at the same instant; only one may settle.

    Each worker uses its own session and commits, released together by a
    barrier, so the two INSERTs genuinely race at the database.
    """
    _open_both(pg_session)

    barrier = threading.Barrier(2)

    def _resolve(path):
        session = get_session()
        try:
            session.connection()
            barrier.wait(timeout=30)
            result = resolve_path(
                session, mandate_id=MANDATE_ID, billing_cycle=CYCLE, path=path
            )
            session.commit()
            return result.status
        except Exception as exc:  # noqa: BLE001 — surfaced as a failed assert below
            session.rollback()
            return f"EXC:{type(exc).__name__}"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(_resolve, ["mandate", "alt_rail"]))

    assert not [s for s in statuses if s.startswith("EXC:")], f"raised: {statuses}"
    assert sorted(statuses) == [AUTO_REFUNDED, SETTLED], f"got {statuses}"
    assert _count(pg_session, SETTLED) == 1
    assert _count(pg_session, AUTO_REFUNDED) == 1

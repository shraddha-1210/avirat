"""Layer 4c — idempotency, proven against a REAL PostgreSQL UNIQUE constraint.

testing.md Phase 4, Test 2 is explicit: "Confirm test runs against a real
unique constraint (test DB/transaction), not a mocked lock — this test is only
meaningful if it hits the actual constraint." Every test here therefore takes
the `pg_session` fixture, which skips loudly rather than substituting SQLite.

Two things make the concurrency real, and both are load-bearing:

* **Each worker opens its OWN session/connection and commits.** Sharing one
  session would serialise the writes in the client and prove nothing about the
  database.
* **A `threading.Barrier` releases every worker at the same instant, after its
  connection is already established.** Without it, `ThreadPoolExecutor` starts
  threads staggered and the first commit lands before the others reach their
  read — the burst passes while never actually colliding. `test_harness_-
  produces_real_contention` is the guard on that: it runs a naive
  SELECT-then-INSERT through the same harness and asserts it DOES break.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text

from db import get_session
from layers.recovery_policy import decide, fire_action_idempotent
from models import ActionsLog

MANDATE_ID = "MND-00042"
BILLING_CYCLE = "2026-09"

# A high-risk, alt-rail-eligible event: the case where a double-fire would mean
# charging a customer twice outside the mandate rail.
_HIGH_RISK = dict(
    cause="mandate_revoked",
    diagnosis_tier=2,
    diagnosis_status="resolved",
    days_to_next_cycle=1,
    mandate_reliability=0.55,
    amount=4800,
)


def _decision():
    return decide(**_HIGH_RISK)


def _count_actions(session) -> int:
    return session.execute(
        select(func.count())
        .select_from(ActionsLog)
        .where(ActionsLog.mandate_id == MANDATE_ID)
        .where(ActionsLog.billing_cycle == BILLING_CYCLE)
    ).scalar_one()


def _fire_once(barrier: threading.Barrier | None = None):
    """One independent worker: own session, own connection, own commit.

    The connection is established BEFORE the barrier so that what the workers
    race on is the INSERT, not connection setup.
    """
    session = get_session()
    try:
        session.connection()
        if barrier is not None:
            barrier.wait(timeout=30)
        fired, action_id = fire_action_idempotent(
            session,
            mandate_id=MANDATE_ID,
            billing_cycle=BILLING_CYCLE,
            decision=_decision(),
        )
        session.commit()
        return fired, action_id
    finally:
        session.close()


def _burst(n: int) -> list:
    """Fire `n` genuinely simultaneous attempts at the same key."""
    barrier = threading.Barrier(n)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda _: _fire_once(barrier), range(n)))


def test_constraint_is_real_and_enforced_by_postgres(pg_session):
    """Guard on the guard: assert the UNIQUE constraint actually exists in the DB.

    If a schema change silently dropped it, every other test in this file would
    still pass while the system was unsafe. This asserts the constraint itself.
    """
    assert pg_session.bind.dialect.name == "postgresql"
    row = pg_session.execute(
        text(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conname = 'uq_actions_mandate_cycle'"
        )
    ).scalar_one_or_none()
    assert row is not None, "uq_actions_mandate_cycle is missing from the live schema"
    assert "UNIQUE" in row and "mandate_id" in row and "billing_cycle" in row


def test_harness_produces_real_contention(pg_session):
    """Negative control: the burst harness must be able to DETECT a double-fire.

    Runs an application-level SELECT-then-INSERT guard — the exact pattern
    `fire_action_idempotent` rejects — through the same barrier harness. It has
    a read-then-write race window, so under genuine contention it must fail
    loudly (unique violations, or more than one row).

    If this test ever passes cleanly, the harness has stopped colliding and
    every other burst assertion below is vacuous.
    """

    def _naive(_):
        session = get_session()
        try:
            session.connection()
            barrier.wait(timeout=30)
            existing = _count_actions(session)
            if existing:
                session.rollback()
                return "skipped"
            session.add(
                ActionsLog(
                    mandate_id=MANDATE_ID,
                    billing_cycle=BILLING_CYCLE,
                    action_type="ALT_RAIL",
                    params={},
                    status="processing",
                )
            )
            session.commit()
            return "inserted"
        except Exception as exc:  # noqa: BLE001 — the race we are demonstrating
            session.rollback()
            return type(exc).__name__
        finally:
            session.close()

    n = 50
    barrier = threading.Barrier(n)
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(_naive, range(n)))

    collisions = [r for r in results if r not in ("inserted", "skipped")]
    assert collisions, (
        "the naive guard survived the burst — threads are not actually colliding, "
        "so the idempotency proof below would be vacuous"
    )


def test_second_identical_insert_is_a_noop(pg_session):
    """Sequential duplicate: first fires, second is a silent no-op (no exception)."""
    fired_1, id_1 = fire_action_idempotent(
        pg_session, mandate_id=MANDATE_ID, billing_cycle=BILLING_CYCLE, decision=_decision()
    )
    pg_session.commit()
    fired_2, id_2 = fire_action_idempotent(
        pg_session, mandate_id=MANDATE_ID, billing_cycle=BILLING_CYCLE, decision=_decision()
    )
    pg_session.commit()

    assert fired_1 is True and id_1 is not None
    assert fired_2 is False and id_2 is None
    assert _count_actions(pg_session) == 1


def test_different_billing_cycle_is_not_blocked(pg_session):
    """The guard must be per-CYCLE: next month's action is a legitimate new row.

    A guard keyed on mandate_id alone would pass the duplicate tests and then
    silently refuse to ever bill the customer again.
    """
    fire_action_idempotent(
        pg_session, mandate_id=MANDATE_ID, billing_cycle="2026-09", decision=_decision()
    )
    fired, action_id = fire_action_idempotent(
        pg_session, mandate_id=MANDATE_ID, billing_cycle="2026-10", decision=_decision()
    )
    pg_session.commit()

    assert fired is True and action_id is not None
    assert pg_session.execute(
        select(func.count()).select_from(ActionsLog).where(ActionsLog.mandate_id == MANDATE_ID)
    ).scalar_one() == 2


def test_100_concurrent_threads_fire_exactly_once(pg_session):
    """plan.md verification: 100 concurrent inserts via thread pool -> only 1 succeeds."""
    results = _burst(100)

    fired = [r for r in results if r[0]]
    assert len(results) == 100
    assert len(fired) == 1, f"expected exactly 1 winner, got {len(fired)}"
    assert _count_actions(pg_session) == 1
    # The 99 losers must return cleanly, not raise and not invent an id.
    assert all(r[1] is None for r in results if not r[0])


def test_50_concurrent_async_burst_fires_exactly_once(pg_session):
    """testing.md Phase 4 Test 2: 50 simultaneous calls via asyncio.gather.

    `to_thread` is deliberate: the driver is sync, so real parallel connections
    come from threads. `return_exceptions=True` means an unhandled error would
    surface as a failed assertion here rather than vanishing into the gather.
    """
    n = 50
    barrier = threading.Barrier(n)

    async def _run():
        loop = asyncio.get_running_loop()
        # An explicit executor is required: `asyncio.to_thread` uses the default
        # one (capped near 32 threads), so a 50-party barrier could never fill
        # and every worker would time out with BrokenBarrierError.
        with ThreadPoolExecutor(max_workers=n) as pool:
            return await asyncio.gather(
                *[loop.run_in_executor(pool, _fire_once, barrier) for _ in range(n)],
                return_exceptions=True,
            )

    results = asyncio.run(_run())

    exceptions = [r for r in results if isinstance(r, BaseException)]
    assert not exceptions, f"unhandled exceptions across the burst: {exceptions[:3]}"

    fired = [r for r in results if r[0]]
    assert len(results) == n
    assert len(fired) == 1, f"expected exactly 1 winner, got {len(fired)}"
    assert _count_actions(pg_session) == 1


def test_burst_records_the_winning_decision_intact(pg_session):
    """The one surviving row must carry a full, auditable decision payload."""
    _burst(20)

    row = pg_session.execute(
        select(ActionsLog)
        .where(ActionsLog.mandate_id == MANDATE_ID)
        .where(ActionsLog.billing_cycle == BILLING_CYCLE)
    ).scalar_one()

    assert row.action_type == "ALT_RAIL"
    assert row.status == "processing"
    assert row.params["cause"] == "mandate_revoked"
    assert row.params["cost_benefit_passed"] is True
    assert row.params["risk_score"] == pytest.approx(_decision().risk.score)
    assert "alt-rail" in row.params["reason"]

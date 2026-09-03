"""Layer 5 — Reconciliation: settlement-hold window, collision auto-refund, expiry escalation.

The failure this layer exists to prevent: Layer 4 fires an alt-rail collection
because the mandate looked dead, and then the mandate rail *also* collects. The
customer has now paid twice for one billing cycle. Reconciliation guarantees
that exactly one path is ever marked `settled` for a key, and that any second
collection inside the settlement-hold window is refunded automatically.

Three entry points:

* `open_hold()`      — a path has been attempted; start its hold. Idempotent.
* `resolve_path()`   — a settlement webhook arrived for one path. This is where
                       the collision is detected and refunded.
* `sweep_expired_holds()` — cron safety valve: a hold that outlived the window
                       with NO path settled is escalated to Ops rather than
                       left ambiguous forever.

**Exactly-one-settled is a database guarantee, not an application one.** Two
webhooks can land concurrently; a `SELECT ... then UPDATE` would let both mark
themselves settled. The partial unique index `uq_recon_single_settled`
(`UNIQUE (mandate_id, billing_cycle) WHERE status = 'settled'`) makes that
impossible, and `resolve_path()` treats the resulting IntegrityError as the
collision signal — the same doctrine as the Layer 4c idempotency guard.

COMPLIANCE — ALT-RAIL PROTOTYPE: the alt-rail path modelled here collects
outside the original e-mandate and requires RBI e-mandate / AFA review before
production use. No real money moves; refunds are recorded to the ledger only.

DEMO SHORTCUT: `_dispatch_refund()` logs and records intent. A production build
would call the PSP's refund API here, with its own idempotency key, and would
reconcile that call's outcome asynchronously.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from models import ActionsLog, OpsEscalationQueue, ReconciliationLedger

logger = logging.getLogger(__name__)

Path = Literal["mandate", "alt_rail"]

PENDING = "pending"
SETTLED = "settled"
AUTO_REFUNDED = "auto_refunded"
EXPIRED_ESCALATED = "expired_escalated"
CLOSED_SUPERSEDED = "closed_superseded"

# A key is only escalated when NOTHING settled. These statuses mean the key
# reached a definite end state and must never be swept again.
_TERMINAL = frozenset({SETTLED, AUTO_REFUNDED, EXPIRED_ESCALATED, CLOSED_SUPERSEDED})


@dataclass(frozen=True)
class ReconResult:
    """Terminal, auditable outcome of one reconciliation step."""

    path: Path
    status: str
    collided_with: Path | None
    refunded_amount: int | None
    within_hold_window: bool
    seconds_since_open: float | None
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SweepResult:
    """What one expiry sweep did, with the cutoff it used."""

    scanned: int
    escalated: int
    superseded: int
    cutoff: datetime
    hold_seconds: int
    escalated_keys: list[tuple[str, str]]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cutoff"] = self.cutoff.isoformat()
        d["escalated_keys"] = [list(k) for k in self.escalated_keys]
        return d


def _dispatch_refund(*, mandate_id: str, billing_cycle: str, path: Path, amount: int) -> None:
    """DEMO SHORTCUT: no money moves. Production would call the PSP refund API."""
    logger.warning(
        "AUTO-REFUND %s path for (%s, %s), amount=%s [mock: no PSP call]",
        path,
        mandate_id,
        billing_cycle,
        amount,
    )


def _close_action(session: Session, *, mandate_id: str, billing_cycle: str, now: datetime) -> None:
    """Stamp the Layer 4 action for this key as resolved.

    Layer 6 measures MTTR from `ActionsLog.created_at` to `resolved_at`, so the
    clock has to be stopped by whichever layer actually reaches a terminal
    state. Reconciliation is that layer for money paths; the TTL watchdog is it
    for abandoned ones. Only the first terminal event wins — a later refund must
    not rewrite an earlier settlement's duration.
    """
    action = session.execute(
        select(ActionsLog)
        .where(ActionsLog.mandate_id == mandate_id)
        .where(ActionsLog.billing_cycle == billing_cycle)
    ).scalar_one_or_none()
    if action is not None and action.resolved_at is None:
        action.resolved_at = now
        if action.status == "processing":
            action.status = "settled"
        session.flush()


def open_hold(
    session: Session,
    *,
    mandate_id: str,
    billing_cycle: str,
    path: Path,
    amount: int,
    now: datetime | None = None,
) -> bool:
    """Open a settlement hold for one path. Returns False if it already existed.

    Idempotent via `uq_recon_key_path`: a retried dispatch must not reset an
    existing hold's clock, which would let a stale attempt dodge expiry forever.
    """
    stmt = (
        pg_insert(ReconciliationLedger)
        .values(
            mandate_id=mandate_id,
            billing_cycle=billing_cycle,
            path=path,
            amount=amount,
            status=PENDING,
            **({"opened_at": now} if now is not None else {}),
        )
        .on_conflict_do_nothing(constraint="uq_recon_key_path")
        .returning(ReconciliationLedger.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def _get_row(session: Session, *, mandate_id: str, billing_cycle: str, path: Path):
    return session.execute(
        select(ReconciliationLedger)
        .where(ReconciliationLedger.mandate_id == mandate_id)
        .where(ReconciliationLedger.billing_cycle == billing_cycle)
        .where(ReconciliationLedger.path == path)
    ).scalar_one_or_none()


def resolve_path(
    session: Session,
    *,
    mandate_id: str,
    billing_cycle: str,
    path: Path,
    amount: int | None = None,
    now: datetime | None = None,
) -> ReconResult:
    """A settlement webhook arrived for `path`. Settle it, or refund the collision.

    The write is attempted inside a SAVEPOINT so that a partial-unique-index
    violation — meaning another path settled first — can be caught and converted
    into a refund WITHOUT poisoning the caller's outer transaction.
    """
    now = now or datetime.now(timezone.utc)

    row = _get_row(session, mandate_id=mandate_id, billing_cycle=billing_cycle, path=path)
    if row is None:
        # A webhook for a path we never attempted. Open the hold implicitly so
        # the money is still recorded rather than silently dropped.
        open_hold(
            session,
            mandate_id=mandate_id,
            billing_cycle=billing_cycle,
            path=path,
            amount=amount or 0,
            now=now,
        )
        row = _get_row(session, mandate_id=mandate_id, billing_cycle=billing_cycle, path=path)

    if row.status in _TERMINAL:
        return ReconResult(
            path=path,
            status=row.status,
            collided_with=None,
            refunded_amount=None,
            within_hold_window=False,
            seconds_since_open=None,
            reason=f"path already terminal ('{row.status}') — webhook replay ignored",
        )

    opened_at = row.opened_at
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    elapsed = (now - opened_at).total_seconds()
    within_window = elapsed <= settings.settlement_hold_seconds

    try:
        with session.begin_nested():        # SAVEPOINT
            row.status = SETTLED
            row.resolved_at = now
            session.flush()
    except IntegrityError:
        # `uq_recon_single_settled` rejected it: another path is already settled
        # for this key. This is the collision the layer exists for.
        session.expire(row)
        other = _settled_path(session, mandate_id=mandate_id, billing_cycle=billing_cycle)
        row = _get_row(session, mandate_id=mandate_id, billing_cycle=billing_cycle, path=path)
        row.status = AUTO_REFUNDED
        row.resolved_at = now
        session.flush()
        _dispatch_refund(
            mandate_id=mandate_id,
            billing_cycle=billing_cycle,
            path=path,
            amount=row.amount,
        )

        if not within_window:
            # The hold had already expired, so an operator may have acted on this
            # key. Refund still happens, but a human is told.
            _escalate(
                session,
                mandate_id=mandate_id,
                billing_cycle=billing_cycle,
                reason="late_collision_after_hold",
            )

        return ReconResult(
            path=path,
            status=AUTO_REFUNDED,
            collided_with=other,
            refunded_amount=row.amount,
            within_hold_window=within_window,
            seconds_since_open=round(elapsed, 3),
            reason=(
                f"collision: '{other}' path already settled for this key "
                f"{elapsed:.1f}s after hold opened (window "
                f"{settings.settlement_hold_seconds}s) — this path auto-refunded"
                + ("" if within_window else "; LATE, escalated to Ops")
            ),
        )

    _close_action(session, mandate_id=mandate_id, billing_cycle=billing_cycle, now=now)
    return ReconResult(
        path=path,
        status=SETTLED,
        collided_with=None,
        refunded_amount=None,
        within_hold_window=within_window,
        seconds_since_open=round(elapsed, 3),
        reason=f"first path to settle for this key ({elapsed:.1f}s after hold opened)",
    )


def _settled_path(session: Session, *, mandate_id: str, billing_cycle: str) -> Path | None:
    return session.execute(
        select(ReconciliationLedger.path)
        .where(ReconciliationLedger.mandate_id == mandate_id)
        .where(ReconciliationLedger.billing_cycle == billing_cycle)
        .where(ReconciliationLedger.status == SETTLED)
    ).scalar_one_or_none()


def _escalate(session: Session, *, mandate_id: str, billing_cycle: str, reason: str) -> None:
    session.add(
        OpsEscalationQueue(
            mandate_id=mandate_id,
            billing_cycle=billing_cycle,
            reason=reason,
            source_layer="reconciliation",
            status="open",
        )
    )
    session.flush()


def sweep_expired_holds(session: Session, *, now: datetime | None = None) -> SweepResult:
    """Close every hold whose settlement window has elapsed. Idempotent.

    Two distinct outcomes, and conflating them would be wrong:

    * **No path settled** -> `expired_escalated` + an Ops row. Money may or may
      not have moved and nobody knows; that ambiguity is exactly what a human
      must resolve.
    * **A sibling path settled** -> `closed_superseded`, silently. This path
      never collected, so there is nothing to refund and nothing to escalate.
      Escalating it would bury the real cases in noise.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.settlement_hold_seconds)

    stale = session.execute(
        select(ReconciliationLedger)
        .where(ReconciliationLedger.status == PENDING)
        .where(ReconciliationLedger.opened_at < cutoff)
    ).scalars().all()

    escalated_keys: list[tuple[str, str]] = []
    superseded = 0
    for row in stale:
        settled = _settled_path(
            session, mandate_id=row.mandate_id, billing_cycle=row.billing_cycle
        )
        if settled is not None:
            row.status = CLOSED_SUPERSEDED
            row.resolved_at = now
            superseded += 1
            logger.info(
                "hold for (%s, %s) path=%s closed: '%s' path already settled",
                row.mandate_id,
                row.billing_cycle,
                row.path,
                settled,
            )
            continue

        row.status = EXPIRED_ESCALATED
        row.resolved_at = now
        _close_action(
            session, mandate_id=row.mandate_id, billing_cycle=row.billing_cycle, now=now
        )
        _escalate(
            session,
            mandate_id=row.mandate_id,
            billing_cycle=row.billing_cycle,
            reason="settlement_hold_expired",
        )
        escalated_keys.append((row.mandate_id, row.billing_cycle))
        logger.warning(
            "settlement hold EXPIRED with no settled path for (%s, %s) path=%s "
            "(opened %s, cutoff %s) — escalated",
            row.mandate_id,
            row.billing_cycle,
            row.path,
            row.opened_at,
            cutoff,
        )

    session.flush()
    return SweepResult(
        scanned=len(stale),
        escalated=len(escalated_keys),
        superseded=superseded,
        cutoff=cutoff,
        hold_seconds=settings.settlement_hold_seconds,
        escalated_keys=escalated_keys,
    )

"""Layer 4e — TTL watchdog: nothing stays 'processing' forever.

An action row is written as `status='processing'` before the recovery attempt
resolves. If the worker dies, the webhook never arrives, or the gateway simply
never answers, that row would sit in `processing` indefinitely — invisible,
un-actioned, and holding the comms mutex. This sweep is the safety valve: any
action older than `ttl_processing_seconds` is moved to `manual_escalation` and
pushed onto the Ops queue.

DEMO SCALE: `run_forever()` is a 60-second polling loop. Production would be
event-driven — a delayed message enqueued alongside the action, cancelled when
the result lands — so the sweep cost does not scale with table size. The cutoff
arithmetic in `sweep_stuck_actions()` is identical either way; only the trigger
differs, which is why the sweep is a plain function the tests call directly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from models import ActionsLog, OpsEscalationQueue

logger = logging.getLogger(__name__)

# Only an in-flight action can be stuck. A settled/refunded/escalated row is
# terminal and must never be re-escalated by a later sweep.
_IN_FLIGHT_STATUS = "processing"
_ESCALATED_STATUS = "manual_escalation"
_ESCALATION_REASON = "ttl_exceeded_processing"


@dataclass(frozen=True)
class SweepResult:
    """What one sweep did, with the cutoff it used — reproducible in an audit."""

    scanned: int
    escalated: int
    cutoff: datetime
    ttl_seconds: int
    escalated_keys: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cutoff"] = self.cutoff.isoformat()
        d["escalated_keys"] = [list(k) for k in self.escalated_keys]
        return d


def sweep_stuck_actions(session: Session, *, now: datetime | None = None) -> SweepResult:
    """Escalate every action stuck in 'processing' past the TTL. Idempotent.

    Re-running immediately escalates nothing further: the first pass moves each
    row off `processing`, so the second pass does not select it. That matters
    because a poller that double-escalates would spam the Ops queue.

    `now` is injectable so tests can drive the clock without sleeping.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.ttl_processing_seconds)

    stuck = session.execute(
        select(ActionsLog)
        .where(ActionsLog.status == _IN_FLIGHT_STATUS)
        .where(ActionsLog.created_at < cutoff)
    ).scalars().all()

    escalated_keys: list[tuple[str, str]] = []
    for action in stuck:
        action.status = _ESCALATED_STATUS
        session.add(
            OpsEscalationQueue(
                mandate_id=action.mandate_id,
                billing_cycle=action.billing_cycle,
                reason=_ESCALATION_REASON,
                source_layer="ttl_watchdog",
                status="open",
            )
        )
        escalated_keys.append((action.mandate_id, action.billing_cycle))
        logger.warning(
            "TTL watchdog escalating (%s, %s): stuck in '%s' since %s (cutoff %s)",
            action.mandate_id,
            action.billing_cycle,
            _IN_FLIGHT_STATUS,
            action.created_at,
            cutoff,
        )

    session.flush()
    return SweepResult(
        scanned=len(stuck),
        escalated=len(escalated_keys),
        cutoff=cutoff,
        ttl_seconds=settings.ttl_processing_seconds,
        escalated_keys=escalated_keys,
    )


def run_forever(*, interval_seconds: int | None = None) -> None:  # pragma: no cover - demo loop
    """DEMO-SCALE polling loop. Production would be event-driven.

    Not covered by tests on purpose: the tests exercise `sweep_stuck_actions()`
    with an injected clock, which is the part that carries the logic. A sweep
    that raises must not kill the loop, so failures are logged and the next
    tick proceeds.
    """
    from db import get_session

    interval = interval_seconds or settings.ttl_watchdog_interval_seconds
    logger.info("TTL watchdog started (interval=%ss, ttl=%ss) [demo-scale poller]",
                interval, settings.ttl_processing_seconds)
    while True:
        try:
            with get_session() as session:
                result = sweep_stuck_actions(session)
                session.commit()
            if result.escalated:
                logger.warning("TTL watchdog escalated %d stuck action(s)", result.escalated)
        except Exception:  # noqa: BLE001 — a bad sweep must not stop the watchdog
            logger.exception("TTL watchdog sweep failed; continuing to next tick")
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    run_forever()

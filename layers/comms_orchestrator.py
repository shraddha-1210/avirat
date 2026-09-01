"""Layer 4d — the single owner of outbound messaging per (mandate_id, billing_cycle).

The failure this prevents: alt-rail fires and the customer gets a "pay here"
link, then the standard reminder job — which knows nothing about the alt rail —
sends "your payment failed, we will retry". Two contradictory messages about
the same rupee. Worse, the customer may pay twice.

The mutex is a database row, not an in-process flag: `communication_state`
carries `UNIQUE (mandate_id, billing_cycle)` and `alt_rail_live`. Every sender
consults it, and `send_standard_reminder()` refuses while the flag is set.

DEMO SHORTCUT: no message is actually delivered. `send_nudge()` records intent
to the database and logs it. A production build would hand off to a real
WhatsApp / SMS provider here, and that call would need its own idempotency key.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models import CommunicationState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommsResult:
    """Whether a message went out, and — when it did not — precisely why."""

    sent: bool
    channel: str
    suppressed_by: str | None
    reminder_count: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _ensure_state(session: Session, *, mandate_id: str, billing_cycle: str) -> CommunicationState:
    """Get-or-create the comms row for this key, race-safely.

    Two concurrent senders can both find no row; `ON CONFLICT DO NOTHING`
    followed by a re-select means the loser reads the winner's row instead of
    raising a unique violation.
    """
    stmt = (
        pg_insert(CommunicationState)
        .values(mandate_id=mandate_id, billing_cycle=billing_cycle, alt_rail_live=False)
        .on_conflict_do_nothing(constraint="uq_comms_key")
    )
    session.execute(stmt)
    session.flush()
    return session.execute(
        select(CommunicationState)
        .where(CommunicationState.mandate_id == mandate_id)
        .where(CommunicationState.billing_cycle == billing_cycle)
    ).scalar_one()


def set_alt_rail_live(session: Session, *, mandate_id: str, billing_cycle: str) -> None:
    """Claim the comms mutex for the alt rail.

    Called immediately after an ALT_RAIL action is recorded. From this point
    `send_standard_reminder()` is a no-op for this key until explicitly cleared.
    """
    state = _ensure_state(session, mandate_id=mandate_id, billing_cycle=billing_cycle)
    state.alt_rail_live = True
    session.flush()
    logger.info("comms mutex claimed by alt-rail for (%s, %s)", mandate_id, billing_cycle)


def clear_alt_rail_live(session: Session, *, mandate_id: str, billing_cycle: str) -> None:
    """Release the mutex once the alt-rail path is settled or refunded."""
    state = _ensure_state(session, mandate_id=mandate_id, billing_cycle=billing_cycle)
    state.alt_rail_live = False
    session.flush()


def is_alt_rail_live(session: Session, *, mandate_id: str, billing_cycle: str) -> bool:
    row = session.execute(
        select(CommunicationState.alt_rail_live)
        .where(CommunicationState.mandate_id == mandate_id)
        .where(CommunicationState.billing_cycle == billing_cycle)
    ).scalar_one_or_none()
    return bool(row)


def send_nudge(
    session: Session,
    *,
    mandate_id: str,
    billing_cycle: str,
    channel: str = "whatsapp",
) -> CommsResult:
    """Send a balance nudge (NUDGE_BALANCE actions).

    Permitted while the alt rail is live: a nudge and an alt-rail link are not
    contradictory — both ask the customer to fund the same payment.

    DEMO SHORTCUT: recorded to the DB and logged, never actually delivered.
    """
    state = _ensure_state(session, mandate_id=mandate_id, billing_cycle=billing_cycle)
    state.reminder_count += 1
    state.last_reminder_at = datetime.now(timezone.utc)
    session.flush()
    logger.info(
        "NUDGE (%s) -> (%s, %s) [mock: not delivered]", channel, mandate_id, billing_cycle
    )
    return CommsResult(
        sent=True,
        channel=channel,
        suppressed_by=None,
        reminder_count=state.reminder_count,
        reason="balance nudge sent (mock delivery)",
    )


def send_standard_reminder(
    session: Session,
    *,
    mandate_id: str,
    billing_cycle: str,
    channel: str = "whatsapp",
) -> CommsResult:
    """The routine "your payment failed" reminder — suppressed while alt-rail is live.

    This is the mutex in action. The reminder job calls this blindly for every
    open failure; the check lives HERE, in the single comms owner, rather than
    being duplicated into every caller.
    """
    state = _ensure_state(session, mandate_id=mandate_id, billing_cycle=billing_cycle)

    if state.alt_rail_live:
        logger.info(
            "standard reminder SUPPRESSED for (%s, %s): alt-rail is live",
            mandate_id,
            billing_cycle,
        )
        return CommsResult(
            sent=False,
            channel=channel,
            suppressed_by="alt_rail_live",
            reminder_count=state.reminder_count,
            reason="alt-rail is live for this key — suppressed to avoid a contradictory message",
        )

    state.reminder_count += 1
    state.last_reminder_at = datetime.now(timezone.utc)
    session.flush()
    logger.info(
        "REMINDER (%s) -> (%s, %s) [mock: not delivered]", channel, mandate_id, billing_cycle
    )
    return CommsResult(
        sent=True,
        channel=channel,
        suppressed_by=None,
        reminder_count=state.reminder_count,
        reason="standard reminder sent (mock delivery)",
    )

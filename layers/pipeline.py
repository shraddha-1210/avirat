"""Phase 4 orchestrator: diagnosis -> policy -> idempotent dispatch -> comms.

This is the seam where the deterministic layers are composed into one decision
about one event. It owns no rules of its own — every branch below delegates to
the layer that owns it — so the audit trail is the concatenation of each
layer's own explanation rather than a story this module invents.

Ordering is deliberate and load-bearing:

1. **Diagnose**, and persist the result whatever it is. A quarantine is a
   recorded outcome, not a dropped event.
2. **Score and map**, producing a `PolicyDecision` with its full arithmetic.
3. **Fire idempotently.** The DB constraint decides whether this is the first
   attempt for the (mandate_id, billing_cycle) key. Everything after this point
   is skipped on a duplicate — including comms, because a replayed webhook must
   not re-message the customer.
4. **Claim the comms mutex and open the Layer 5 settlement hold** only if the
   action that actually won was ALT_RAIL.

The caller commits. Nothing here commits on its own, so a failure part-way
leaves no half-applied decision.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

import store
from layers.comms_orchestrator import send_nudge, set_alt_rail_live
from layers.diagnosis import diagnose
from layers.reconciliation import open_hold
from layers.recovery_policy import PolicyDecision, decide, fire_action_idempotent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryOutcome:
    """One event's complete journey, reconstructible end to end."""

    event_id: str
    mandate_id: str
    billing_cycle: str
    diagnosis: dict
    decision: dict
    fired: bool
    action_id: int | None
    quarantined: bool
    comms: dict | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_recovery(
    session: Session,
    *,
    event_id: str,
    mandate_id: str,
    billing_cycle: str,
    raw_error_code: str,
    amount: int,
    mandate_reliability: float,
    days_to_next_cycle: int,
    use_cache: bool = True,
) -> RecoveryOutcome:
    """Diagnose one decline event and dispatch exactly one recovery action."""
    # --- 1. diagnose (Layer 3) ------------------------------------------
    diagnosis = diagnose(raw_error_code, use_cache=use_cache)
    store.insert_diagnosis(session, event_id=event_id, result=diagnosis)

    quarantined = diagnosis.status == "QUARANTINE"
    if quarantined:
        store.insert_quarantine(session, event_id=event_id, result=diagnosis)

    # --- 2. score + map (Layer 4a/4b) -----------------------------------
    decision: PolicyDecision = decide(
        cause=diagnosis.cause,
        diagnosis_tier=diagnosis.tier,
        diagnosis_status=diagnosis.status,
        days_to_next_cycle=days_to_next_cycle,
        mandate_reliability=mandate_reliability,
        amount=amount,
    )

    # --- 3. fire, exactly once (Layer 4c) -------------------------------
    fired, action_id = fire_action_idempotent(
        session,
        mandate_id=mandate_id,
        billing_cycle=billing_cycle,
        decision=decision,
    )

    if not fired:
        # A replayed webhook. The action already exists, so no comms either —
        # re-messaging the customer is exactly the duplicate this prevents.
        return RecoveryOutcome(
            event_id=event_id,
            mandate_id=mandate_id,
            billing_cycle=billing_cycle,
            diagnosis=diagnosis.to_dict(),
            decision=decision.to_dict(),
            fired=False,
            action_id=None,
            quarantined=quarantined,
            comms=None,
            reason=(
                f"idempotent guard: an action already exists for "
                f"({mandate_id}, {billing_cycle}) — no dispatch, no comms"
            ),
        )

    # --- 4. comms (Layer 4d), keyed off the action that actually won -----
    comms = None
    if decision.action == "ALT_RAIL":
        # Claim the mutex so the standard reminder job stays silent while the
        # customer holds an alt-rail payment link.
        set_alt_rail_live(session, mandate_id=mandate_id, billing_cycle=billing_cycle)
        comms = {"mutex": "alt_rail_live", "claimed": True}
        # Layer 5: open the settlement hold at dispatch, not at settlement. The
        # window has to start counting from the moment money could move on this
        # path, otherwise a collision arriving before any webhook has no hold to
        # collide with.
        open_hold(
            session,
            mandate_id=mandate_id,
            billing_cycle=billing_cycle,
            path="alt_rail",
            amount=amount,
        )
    elif decision.action == "NUDGE_BALANCE":
        comms = send_nudge(
            session, mandate_id=mandate_id, billing_cycle=billing_cycle
        ).to_dict()

    logger.info(
        "recovery: event=%s tier=%s cause=%s -> %s (fired=%s)",
        event_id,
        diagnosis.tier,
        diagnosis.cause,
        decision.action,
        fired,
    )

    return RecoveryOutcome(
        event_id=event_id,
        mandate_id=mandate_id,
        billing_cycle=billing_cycle,
        diagnosis=diagnosis.to_dict(),
        decision=decision.to_dict(),
        fired=True,
        action_id=action_id,
        quarantined=quarantined,
        comms=comms,
        reason=decision.reason,
    )

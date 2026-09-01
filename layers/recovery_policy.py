"""Layer 4 — Recovery policy: deterministic risk scoring, action mapping, and
database-level idempotent dispatch.

Three separable concerns, deliberately kept apart so each is testable alone:

* **4a `score_recovery_risk()`** — a weighted scorecard over four normalised
  signals. Pure arithmetic: same inputs always yield the same score, and every
  component is returned alongside the total so a decision can be re-derived by
  hand in an audit. No model, no learned weights.
* **4b `map_diagnosis_to_action()`** — a fixed diagnosis -> action table. The
  cause decides the *kind* of action; the risk score decides only whether the
  alt-rail escalation is permitted.
* **4c `fire_action_idempotent()`** — the money-movement seam. Exactly-once is
  enforced by the `uq_actions_mandate_cycle` UNIQUE constraint via
  `INSERT ... ON CONFLICT DO NOTHING`, never an application lock.

COMPLIANCE — ALT-RAIL PROTOTYPE: `ALT_RAIL` models a UPI Intent collection
outside the original e-mandate. It is a PROTOTYPE and requires RBI e-mandate /
AFA (Additional Factor of Authentication) review before any production use. No
code here moves real money; dispatch is recorded to the database only.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Literal

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import settings
from models import ActionsLog

logger = logging.getLogger(__name__)

ActionType = Literal["RETRY", "NUDGE_BALANCE", "ALT_RAIL", "SAFE_HOLD", "MANUAL_REVIEW"]

# Alt-rail is the only action that collects money outside the mandate rail, so
# it is the only one gated on the risk score AND the cost-benefit test.
_ALT_RAIL_ELIGIBLE_CAUSES: frozenset[str] = frozenset(
    {"mandate_revoked", "mandate_paused", "authentication_failure"}
)

# 4b: cause -> action. A cause absent from this table is not guessed at; it
# falls to MANUAL_REVIEW, which is a person, not a payment.
_CAUSE_TO_ACTION: dict[str, ActionType] = {
    "bank_downtime": "RETRY",
    "technical_decline": "RETRY",
    "insufficient_funds": "NUDGE_BALANCE",
    "payer_limit_exceeded": "NUDGE_BALANCE",
    "mandate_revoked": "ALT_RAIL",
    "mandate_paused": "ALT_RAIL",
    "authentication_failure": "ALT_RAIL",
}

# Rupee cost of one alt-rail attempt (gateway + comms). Illustrative demo value;
# requires finance sign-off before production use.
ALT_RAIL_COST_RUPEES: float = 12.0

# Amount normalisation ceiling: amounts at or above this are the top tier.
# Illustrative demo value; retune on the real amount distribution.
_AMOUNT_TIER_CEILING: float = 5000.0

# Urgency horizon: a failure this many days from the next cycle scores 0 urgency.
_URGENCY_HORIZON_DAYS: float = 30.0


@dataclass(frozen=True)
class RiskScore:
    """Every component that produced `score`, retained for the audit trail."""

    score: float
    urgency: float
    unreliability: float
    amount_tier: float
    cost_benefit: float
    expected_loss: float
    alt_rail_cost: float
    cost_benefit_passed: bool
    components: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    """Terminal, auditable output of one policy evaluation."""

    action: ActionType
    risk: RiskScore
    cause: str | None
    diagnosis_tier: int
    max_retries: int
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["risk"] = self.risk.to_dict()
        return d


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def score_recovery_risk(
    *,
    days_to_next_cycle: int,
    mandate_reliability: float,
    amount: int,
    expected_recovery_rate: float = 0.6,
) -> RiskScore:
    """4a — deterministic weighted scorecard. Returns every component.

    Signals, each normalised to [0, 1] so the weights are comparable:

    * `urgency`        — closeness of the next billing cycle. A failure 1 day
                         out is urgent; 30+ days out is not.
    * `unreliability`  — 1 - reliability. A historically flaky mandate is
                         riskier than a reliable one that just blipped.
    * `amount_tier`    — amount normalised against a 5,000-rupee ceiling.
    * `cost_benefit`   — expected loss vs the cost of an alt-rail attempt.

    `cost_benefit_passed` is a HARD gate, reported separately from the score: a
    high score alone must never fire the alt rail (plan.md, Decisions).
    """
    urgency = _clamp01(1.0 - (max(0, int(days_to_next_cycle)) / _URGENCY_HORIZON_DAYS))
    unreliability = _clamp01(1.0 - float(mandate_reliability))
    amount_tier = _clamp01(float(amount) / _AMOUNT_TIER_CEILING)

    # Expected loss if we do nothing: the share of this amount we would not
    # otherwise recover. Deliberately simple and inspectable.
    expected_loss = float(amount) * _clamp01(1.0 - expected_recovery_rate)
    cost_benefit_passed = expected_loss > ALT_RAIL_COST_RUPEES
    # Normalised benefit ratio, capped so one huge amount cannot dominate.
    cost_benefit = _clamp01(expected_loss / (ALT_RAIL_COST_RUPEES * 20.0))

    score = (
        settings.risk_weight_urgency * urgency
        + settings.risk_weight_reliability * unreliability
        + settings.risk_weight_amount * amount_tier
        + settings.risk_weight_cost_benefit * cost_benefit
    )

    return RiskScore(
        score=round(score, 4),
        urgency=round(urgency, 4),
        unreliability=round(unreliability, 4),
        amount_tier=round(amount_tier, 4),
        cost_benefit=round(cost_benefit, 4),
        expected_loss=round(expected_loss, 2),
        alt_rail_cost=ALT_RAIL_COST_RUPEES,
        cost_benefit_passed=cost_benefit_passed,
        components={
            "w_urgency": settings.risk_weight_urgency,
            "w_reliability": settings.risk_weight_reliability,
            "w_amount": settings.risk_weight_amount,
            "w_cost_benefit": settings.risk_weight_cost_benefit,
            "firing_threshold": settings.risk_firing_threshold,
        },
    )


def map_diagnosis_to_action(
    *,
    cause: str | None,
    diagnosis_tier: int,
    diagnosis_status: str,
    risk: RiskScore,
) -> PolicyDecision:
    """4b — cause -> action, with alt-rail gated on score AND cost-benefit.

    Quarantined events (Tier 3) never receive an automated money action: an
    undiagnosed failure is a person's decision, so it routes to MANUAL_REVIEW.
    """
    if diagnosis_status == "QUARANTINE" or cause is None:
        return PolicyDecision(
            action="MANUAL_REVIEW",
            risk=risk,
            cause=cause,
            diagnosis_tier=diagnosis_tier,
            max_retries=0,
            reason="diagnosis quarantined — no automated action on an undiagnosed failure",
        )

    mapped = _CAUSE_TO_ACTION.get(cause)
    if mapped is None:
        return PolicyDecision(
            action="MANUAL_REVIEW",
            risk=risk,
            cause=cause,
            diagnosis_tier=diagnosis_tier,
            max_retries=0,
            reason=f"cause '{cause}' has no mapped action",
        )

    if mapped != "ALT_RAIL":
        return PolicyDecision(
            action=mapped,
            risk=risk,
            cause=cause,
            diagnosis_tier=diagnosis_tier,
            max_retries=settings.max_retries if mapped == "RETRY" else 0,
            reason=f"cause '{cause}' maps to {mapped}",
        )

    # --- alt-rail gate: BOTH conditions required, never urgency alone --------
    score_ok = risk.score >= settings.risk_firing_threshold
    if score_ok and risk.cost_benefit_passed and cause in _ALT_RAIL_ELIGIBLE_CAUSES:
        return PolicyDecision(
            action="ALT_RAIL",
            risk=risk,
            cause=cause,
            diagnosis_tier=diagnosis_tier,
            max_retries=0,
            reason=(
                f"alt-rail: score {risk.score:.3f} >= {settings.risk_firing_threshold:.2f} "
                f"AND expected loss {risk.expected_loss:.2f} > cost {risk.alt_rail_cost:.2f}"
            ),
        )

    failed = "score" if not score_ok else "cost-benefit"
    return PolicyDecision(
        action="SAFE_HOLD",
        risk=risk,
        cause=cause,
        diagnosis_tier=diagnosis_tier,
        max_retries=0,
        reason=(
            f"alt-rail withheld ({failed} gate): score {risk.score:.3f} vs threshold "
            f"{settings.risk_firing_threshold:.2f}, expected loss {risk.expected_loss:.2f} "
            f"vs cost {risk.alt_rail_cost:.2f} — falling back to SAFE_HOLD"
        ),
    )


def decide(
    *,
    cause: str | None,
    diagnosis_tier: int,
    diagnosis_status: str,
    days_to_next_cycle: int,
    mandate_reliability: float,
    amount: int,
) -> PolicyDecision:
    """Convenience: score then map, in one deterministic call."""
    risk = score_recovery_risk(
        days_to_next_cycle=days_to_next_cycle,
        mandate_reliability=mandate_reliability,
        amount=amount,
    )
    return map_diagnosis_to_action(
        cause=cause,
        diagnosis_tier=diagnosis_tier,
        diagnosis_status=diagnosis_status,
        risk=risk,
    )


# ---------------------------------------------------------------------------
# 4c — idempotent dispatch. THE money-movement seam.
# ---------------------------------------------------------------------------
def fire_action_idempotent(
    session: Session,
    *,
    mandate_id: str,
    billing_cycle: str,
    decision: PolicyDecision,
) -> tuple[bool, int | None]:
    """Record exactly one action per (mandate_id, billing_cycle). Returns (fired, id).

    Exactly-once comes from the DATABASE: `uq_actions_mandate_cycle` plus
    `ON CONFLICT DO NOTHING ... RETURNING id`. Under a concurrent burst every
    caller issues the same statement and Postgres serialises them — exactly one
    gets a row back, the rest get None and return `(False, None)` without
    raising. There is no read-then-write window to lose, which is precisely why
    this is not a `SELECT`-then-`INSERT` guard.

    The caller must commit; the burst test commits per-session to make the
    contention real.
    """
    stmt = (
        pg_insert(ActionsLog)
        .values(
            mandate_id=mandate_id,
            billing_cycle=billing_cycle,
            action_type=decision.action,
            params={
                "cause": decision.cause,
                "diagnosis_tier": decision.diagnosis_tier,
                "risk_score": decision.risk.score,
                "cost_benefit_passed": decision.risk.cost_benefit_passed,
                "max_retries": decision.max_retries,
                "reason": decision.reason,
            },
            status="processing",
        )
        .on_conflict_do_nothing(constraint="uq_actions_mandate_cycle")
        .returning(ActionsLog.id)
    )
    action_id = session.execute(stmt).scalar_one_or_none()

    if action_id is None:
        logger.info(
            "idempotent guard: action already exists for (%s, %s) — no-op",
            mandate_id,
            billing_cycle,
        )
        return False, None
    return True, action_id

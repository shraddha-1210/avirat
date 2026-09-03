"""Layer 6 — Metrics. Deterministic aggregation over what the pipeline actually recorded.

Every number here is computed from database rows, never estimated, extrapolated
or back-filled. Where a metric cannot honestly be computed — no resolved
actions, no arm assignment, an empty control group — it returns `None` with a
stated reason rather than a zero, because a zero on a dashboard reads as a
measurement and `None` reads as "not measured".

Two honesty rules the shape of this module enforces:

* **MTTR excludes in-flight work.** An action with no `resolved_at` has not
  taken zero time; it has taken an unknown time. Counting it as 0 would make
  MTTR fall as the backlog grows, which is exactly backwards.
* **Rupees Recovered is a controlled A/B difference, and it is reported with
  the arm sizes beside it.** A raw Treatment - Control subtraction is only
  meaningful when the arms are comparable, so `per_mandate_delta` is reported
  alongside and the caller can see both.

CONTROLLED SIMULATION: these figures come from a fixed synthetic seed. They are
not production measurements and the dashboard says so.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.orm import Session

from models import (
    ActionsLog,
    DeclineEvent,
    DetectedAnomaly,
    Diagnosis,
    OpsEscalationQueue,
    QuarantineQueue,
    ReconciliationLedger,
)

# An action is "recovered quickly" if it reached a terminal state inside this.
RECOVERY_SLA_HOURS = 24

# Statuses on the ledger that mean money was actually collected and kept.
_MONEY_KEPT = ("settled",)


@dataclass(frozen=True)
class TierMTTR:
    tier: int
    resolved_count: int
    in_flight_count: int
    mttr_seconds: float | None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RecoveredRupees:
    """Treatment - Control, with everything needed to judge whether that is fair."""

    treatment_recovered: int
    control_recovered: int
    delta: int
    treatment_mandates: int
    control_mandates: int
    per_mandate_delta: float | None
    computable: bool
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MetricsSummary:
    mttr_by_tier: list[dict]
    recovered: dict
    recovery_rate: dict
    escalations_by_type: list[dict]
    quarantine: dict
    detection: dict
    actions_by_type: list[dict]
    ledger_by_status: list[dict]
    ops_queue: list[dict]
    safe_hold_cases: list[dict]
    quarantine_cases: list[dict]
    reconciliation_races: list[dict]
    generated_at: str
    disclaimer: str = (
        "Controlled simulation on a fixed synthetic seed - not live production data."
    )
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _seconds(expr):
    """Postgres interval -> float seconds."""
    return cast(func.extract("epoch", expr), Float)


def mttr_by_tier(session: Session) -> list[TierMTTR]:
    """Mean time from action dispatch to terminal state, grouped by diagnosis tier.

    The join runs Diagnosis -> DeclineEvent -> ActionsLog because diagnoses are
    keyed by `event_id` while actions are keyed by `(mandate_id, billing_cycle)`.

    Unresolved actions are counted separately and EXCLUDED from the mean; see
    the module docstring for why averaging them in as zero would invert the
    metric's meaning.
    """
    rows = session.execute(
        select(
            Diagnosis.tier,
            func.count().label("total"),
            func.count(ActionsLog.resolved_at).label("resolved"),
            func.avg(
                case(
                    (
                        ActionsLog.resolved_at.isnot(None),
                        _seconds(ActionsLog.resolved_at - ActionsLog.created_at),
                    ),
                    else_=None,
                )
            ).label("mttr"),
        )
        .select_from(Diagnosis)
        .join(DeclineEvent, DeclineEvent.event_id == Diagnosis.event_id)
        .join(
            ActionsLog,
            (ActionsLog.mandate_id == DeclineEvent.mandate_id)
            & (ActionsLog.billing_cycle == DeclineEvent.billing_cycle),
        )
        .group_by(Diagnosis.tier)
        .order_by(Diagnosis.tier)
    ).all()

    out: list[TierMTTR] = []
    for tier, total, resolved, mttr in rows:
        out.append(
            TierMTTR(
                tier=int(tier),
                resolved_count=int(resolved),
                in_flight_count=int(total) - int(resolved),
                mttr_seconds=round(float(mttr), 3) if mttr is not None else None,
                note="" if resolved else "no resolved actions yet - MTTR not measurable",
            )
        )
    return out


def recovered_rupees(session: Session) -> RecoveredRupees:
    """Rupees Recovered = Treatment settled sum - Control settled sum.

    Requires `decline_events.arm` to be populated. If no event carries an arm,
    the metric is not computable and says so instead of returning 0, which
    would be indistinguishable from "the intervention did nothing".
    """
    rows = session.execute(
        select(
            DeclineEvent.arm,
            func.coalesce(func.sum(ReconciliationLedger.amount), 0).label("recovered"),
            func.count(func.distinct(DeclineEvent.mandate_id)).label("mandates"),
        )
        .select_from(DeclineEvent)
        .join(
            ReconciliationLedger,
            (ReconciliationLedger.mandate_id == DeclineEvent.mandate_id)
            & (ReconciliationLedger.billing_cycle == DeclineEvent.billing_cycle)
            & (ReconciliationLedger.status.in_(_MONEY_KEPT)),
        )
        .where(DeclineEvent.arm.isnot(None))
        .group_by(DeclineEvent.arm)
    ).all()

    by_arm = {arm: (int(rec), int(m)) for arm, rec, m in rows}
    t_rec, t_man = by_arm.get("treatment", (0, 0))
    c_rec, c_man = by_arm.get("control", (0, 0))

    # Arm sizes come from the full event table, not just settled rows: a
    # mandate that recovered nothing is still part of its arm's denominator.
    arm_sizes = dict(
        session.execute(
            select(DeclineEvent.arm, func.count(func.distinct(DeclineEvent.mandate_id)))
            .where(DeclineEvent.arm.isnot(None))
            .group_by(DeclineEvent.arm)
        ).all()
    )
    t_man = int(arm_sizes.get("treatment", t_man))
    c_man = int(arm_sizes.get("control", c_man))

    if not arm_sizes:
        return RecoveredRupees(
            treatment_recovered=0,
            control_recovered=0,
            delta=0,
            treatment_mandates=0,
            control_mandates=0,
            per_mandate_delta=None,
            computable=False,
            note=(
                "no decline_events carry an arm assignment - run the treatment/control "
                "split before reading this metric; 0 here would be misleading"
            ),
        )

    per_mandate = None
    note = "Treatment - Control on settled ledger amounts."
    if t_man and c_man:
        per_mandate = round((t_rec / t_man) - (c_rec / c_man), 2)
        note += " Arms differ in size, so per_mandate_delta is the comparable figure."
    else:
        note += " One arm is empty - the raw delta is not a controlled comparison."

    return RecoveredRupees(
        treatment_recovered=t_rec,
        control_recovered=c_rec,
        delta=t_rec - c_rec,
        treatment_mandates=t_man,
        control_mandates=c_man,
        per_mandate_delta=per_mandate,
        computable=True,
        note=note,
    )


def recovery_rate(session: Session, *, sla_hours: int = RECOVERY_SLA_HOURS) -> dict:
    """Share of dispatched actions that reached a terminal state within the SLA."""
    total, resolved, within = session.execute(
        select(
            func.count(),
            func.count(ActionsLog.resolved_at),
            func.count(
                case(
                    (
                        ActionsLog.resolved_at.isnot(None)
                        & (
                            _seconds(ActionsLog.resolved_at - ActionsLog.created_at)
                            <= sla_hours * 3600
                        ),
                        1,
                    ),
                    else_=None,
                )
            ),
        ).select_from(ActionsLog)
    ).one()

    total, resolved, within = int(total), int(resolved), int(within)
    return {
        "sla_hours": sla_hours,
        "actions_total": total,
        "actions_resolved": resolved,
        "actions_in_flight": total - resolved,
        "resolved_within_sla": within,
        # Denominator is ALL dispatched actions, not just resolved ones: an
        # action still stuck after the SLA has missed it, and dividing by
        # resolved-only would flatter the number.
        "rate": round(within / total, 4) if total else None,
        "note": "" if total else "no actions dispatched yet",
    }


def escalations_by_type(session: Session) -> list[dict]:
    rows = session.execute(
        select(
            OpsEscalationQueue.source_layer,
            OpsEscalationQueue.reason,
            OpsEscalationQueue.status,
            func.count().label("n"),
        )
        .group_by(
            OpsEscalationQueue.source_layer,
            OpsEscalationQueue.reason,
            OpsEscalationQueue.status,
        )
        .order_by(func.count().desc())
    ).all()
    return [
        {"source_layer": s, "reason": r, "status": st, "count": int(n)}
        for s, r, st, n in rows
    ]


def quarantine_backlog(session: Session, *, sample: int = 5) -> dict:
    total = session.execute(
        select(func.count()).select_from(QuarantineQueue)
    ).scalar_one()
    pending = session.execute(
        select(func.count())
        .select_from(QuarantineQueue)
        .where(QuarantineQueue.status == "pending_ops_review")
    ).scalar_one()
    rows = session.execute(
        select(QuarantineQueue)
        .where(QuarantineQueue.status == "pending_ops_review")
        .order_by(QuarantineQueue.created_at.desc())
        .limit(sample)
    ).scalars().all()
    return {
        "total": int(total),
        "pending_ops_review": int(pending),
        "samples": [
            {
                "event_id": r.event_id,
                "raw_input": r.raw_input,
                "tier_attempted": r.tier_attempted,
                "reason": r.reason,
            }
            for r in rows
        ],
    }


def detection_stats(session: Session, *, hours: int = 24, now: datetime | None = None) -> dict:
    """Segment-wise MAD values and how many anomalies fired recently."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    rows = session.execute(
        select(
            DetectedAnomaly.segment,
            func.count().label("checks"),
            func.sum(case((DetectedAnomaly.is_anomaly.is_(True), 1), else_=0)).label("flags"),
            func.avg(DetectedAnomaly.mad).label("avg_mad"),
            func.max(DetectedAnomaly.threshold).label("threshold"),
        )
        .group_by(DetectedAnomaly.segment)
        .order_by(DetectedAnomaly.segment)
    ).all()

    recent = session.execute(
        select(func.count())
        .select_from(DetectedAnomaly)
        .where(DetectedAnomaly.is_anomaly.is_(True))
        .where(DetectedAnomaly.detected_at >= since)
    ).scalar_one()

    return {
        "window_hours": hours,
        "flags_in_window": int(recent),
        "segments": [
            {
                "segment": seg,
                "checks": int(checks),
                "flags": int(flags or 0),
                "avg_mad": round(float(mad), 4) if mad is not None else None,
                "threshold": round(float(thr), 4) if thr is not None else None,
            }
            for seg, checks, flags, mad, thr in rows
        ],
    }


def ops_queue(session: Session, *, limit: int = 200, now: datetime | None = None) -> list[dict]:
    """Individual Ops cases with their age, for the queue screen.

    `escalations_by_type` aggregates; this returns the rows themselves so the UI
    can sort by age and show an SLA countdown. Open cases first, oldest first.
    """
    now = now or datetime.now(timezone.utc)
    rows = session.execute(
        select(OpsEscalationQueue)
        .order_by(
            case((OpsEscalationQueue.status == "open", 0), else_=1),
            OpsEscalationQueue.created_at.asc(),
        )
        .limit(limit)
    ).scalars().all()

    out = []
    for r in rows:
        created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds()
        out.append(
            {
                "id": r.id,
                "mandate_id": r.mandate_id,
                "billing_cycle": r.billing_cycle,
                "reason": r.reason,
                "source_layer": r.source_layer,
                "status": r.status,
                "created_at": created.isoformat(),
                "age_seconds": round(age, 1),
                # Negative means the SLA is already blown; the UI shows it in red.
                "sla_remaining_seconds": round(RECOVERY_SLA_HOURS * 3600 - age, 1),
            }
        )
    return out


def safe_hold_cases(session: Session, *, limit: int = 200, now: datetime | None = None) -> list[dict]:
    """SAFE_HOLD and MANUAL_REVIEW actions — decided, but deliberately not acted on.

    These are the other half of the Ops queue: the policy withheld the alt rail,
    so a human decides whether to escalate. Paired with the quarantine backlog in
    the UI.
    """
    now = now or datetime.now(timezone.utc)
    rows = session.execute(
        select(ActionsLog)
        .where(ActionsLog.action_type.in_(("SAFE_HOLD", "MANUAL_REVIEW")))
        .order_by(ActionsLog.created_at.asc())
        .limit(limit)
    ).scalars().all()

    out = []
    for r in rows:
        created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds()
        params = r.params or {}
        out.append(
            {
                "id": r.id,
                "mandate_id": r.mandate_id,
                "billing_cycle": r.billing_cycle,
                "action_type": r.action_type,
                "status": r.status,
                "cause": params.get("cause"),
                "diagnosis_tier": params.get("diagnosis_tier"),
                "risk_score": params.get("risk_score"),
                "reason": params.get("reason", ""),
                "created_at": created.isoformat(),
                "age_seconds": round(age, 1),
                "sla_remaining_seconds": round(RECOVERY_SLA_HOURS * 3600 - age, 1),
            }
        )
    return out


def quarantine_cases(session: Session, *, limit: int = 200) -> list[dict]:
    """Every quarantined string with its age, for the promote-to-rule workflow."""
    rows = session.execute(
        select(QuarantineQueue)
        .order_by(QuarantineQueue.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "id": r.id,
            "event_id": r.event_id,
            "raw_input": r.raw_input,
            "tier_attempted": r.tier_attempted,
            "reason": r.reason,
            "status": r.status,
            "created_at": (
                r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
            ).isoformat(),
        }
        for r in rows
    ]


def reconciliation_races(session: Session, *, limit: int = 100) -> list[dict]:
    """Keys where BOTH rails opened a hold — the double-charge race, per key.

    A key with one path never raced. These are the ones worth showing: which
    rail won, which was auto-refunded, and how far apart they landed.
    """
    keyed: dict[tuple[str, str], list] = {}
    for r in session.execute(
        select(ReconciliationLedger).order_by(ReconciliationLedger.opened_at.asc())
    ).scalars().all():
        keyed.setdefault((r.mandate_id, r.billing_cycle), []).append(r)

    races = []
    for (mandate_id, billing_cycle), rows in keyed.items():
        if len(rows) < 2:
            continue
        paths = {
            r.path: {
                "status": r.status,
                "amount": r.amount,
                "opened_at": r.opened_at.isoformat(),
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            }
            for r in rows
        }
        settled = [r.path for r in rows if r.status == "settled"]
        refunded = [r.path for r in rows if r.status == "auto_refunded"]
        resolved_times = [r.resolved_at for r in rows if r.resolved_at]
        gap = None
        if len(resolved_times) >= 2:
            ordered = sorted(resolved_times)
            gap = round((ordered[-1] - ordered[0]).total_seconds(), 1)
        races.append(
            {
                "mandate_id": mandate_id,
                "billing_cycle": billing_cycle,
                "paths": paths,
                "winner": settled[0] if settled else None,
                "refunded": refunded[0] if refunded else None,
                "collision": bool(settled and refunded),
                "gap_seconds": gap,
            }
        )
        if len(races) >= limit:
            break
    return races


def _count_by(session: Session, column) -> list[dict]:
    rows = session.execute(
        select(column, func.count()).group_by(column).order_by(func.count().desc())
    ).all()
    return [{"key": k, "count": int(n)} for k, n in rows]


def compute_metrics(session: Session, *, now: datetime | None = None) -> MetricsSummary:
    """Everything the dashboard needs, in one deterministic pass."""
    now = now or datetime.now(timezone.utc)
    return MetricsSummary(
        mttr_by_tier=[t.to_dict() for t in mttr_by_tier(session)],
        recovered=recovered_rupees(session).to_dict(),
        recovery_rate=recovery_rate(session),
        escalations_by_type=escalations_by_type(session),
        quarantine=quarantine_backlog(session),
        detection=detection_stats(session, now=now),
        actions_by_type=_count_by(session, ActionsLog.action_type),
        ledger_by_status=_count_by(session, ReconciliationLedger.status),
        ops_queue=ops_queue(session, now=now),
        safe_hold_cases=safe_hold_cases(session, now=now),
        quarantine_cases=quarantine_cases(session),
        reconciliation_races=reconciliation_races(session),
        generated_at=now.isoformat(),
    )


def decision_trace(session: Session, *, mandate_id: str, billing_cycle: str) -> dict:
    """Full audit trail for one (mandate_id, billing_cycle).

    Answers "why did this customer get charged this way?" from stored rows
    only. Every layer that touched the key contributes its own recorded
    explanation; nothing is reconstructed after the fact.
    """
    events = session.execute(
        select(DeclineEvent)
        .where(DeclineEvent.mandate_id == mandate_id)
        .where(DeclineEvent.billing_cycle == billing_cycle)
        .order_by(DeclineEvent.event_ts)
    ).scalars().all()

    diagnoses = []
    if events:
        diagnoses = session.execute(
            select(Diagnosis)
            .where(Diagnosis.event_id.in_([e.event_id for e in events]))
            .order_by(Diagnosis.created_at)
        ).scalars().all()

    action = session.execute(
        select(ActionsLog)
        .where(ActionsLog.mandate_id == mandate_id)
        .where(ActionsLog.billing_cycle == billing_cycle)
    ).scalar_one_or_none()

    ledger = session.execute(
        select(ReconciliationLedger)
        .where(ReconciliationLedger.mandate_id == mandate_id)
        .where(ReconciliationLedger.billing_cycle == billing_cycle)
        .order_by(ReconciliationLedger.path)
    ).scalars().all()

    escalations = session.execute(
        select(OpsEscalationQueue)
        .where(OpsEscalationQueue.mandate_id == mandate_id)
        .where(OpsEscalationQueue.billing_cycle == billing_cycle)
        .order_by(OpsEscalationQueue.created_at)
    ).scalars().all()

    return {
        "mandate_id": mandate_id,
        "billing_cycle": billing_cycle,
        "found": bool(events or action or ledger),
        "events": [
            {
                "event_id": e.event_id,
                "event_ts": e.event_ts.isoformat(),
                "segment": e.segment,
                "amount": e.amount,
                "raw_error_code": e.raw_error_code,
                "arm": e.arm,
            }
            for e in events
        ],
        "diagnoses": [
            {
                "event_id": d.event_id,
                "tier": d.tier,
                "cause": d.cause,
                "confidence": d.confidence,
                "status": d.status,
                "sanitized_input": d.sanitized_input,
                "llm_model": d.llm_model,
                "at": d.created_at.isoformat(),
            }
            for d in diagnoses
        ],
        "action": (
            {
                "action_type": action.action_type,
                "status": action.status,
                "params": action.params,
                "created_at": action.created_at.isoformat(),
                "resolved_at": action.resolved_at.isoformat() if action.resolved_at else None,
            }
            if action
            else None
        ),
        "reconciliation": [
            {
                "path": r.path,
                "status": r.status,
                "amount": r.amount,
                "opened_at": r.opened_at.isoformat(),
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            }
            for r in ledger
        ],
        "escalations": [
            {
                "reason": e.reason,
                "source_layer": e.source_layer,
                "status": e.status,
                "at": e.created_at.isoformat(),
            }
            for e in escalations
        ],
    }

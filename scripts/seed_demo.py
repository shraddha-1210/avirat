r"""Drive the seeded 240-event dataset through the whole pipeline into Postgres.

Gives Layer 6 something real to measure. Every row the dashboard shows is
produced by the actual layers — ingestion -> detection -> diagnosis -> policy ->
idempotent dispatch -> reconciliation — not by fixtures written to look plausible.

    docker compose up -d db
    .venv\Scripts\python scripts\seed_demo.py

By default Tier 2 uses a DETERMINISTIC OFFLINE STUB so the seeder is free and
reproducible; pass --live to make real Gemini calls instead (needs
GOOGLE_API_KEY; costs one call per distinct ambiguous string).

The treatment/control split is the honest part: control-arm mandates are
ingested and diagnosed but NEVER receive a recovery action, so the Layer 6
"Rupees Recovered" delta measures the intervention rather than restating it.
"""
from __future__ import annotations

import argparse
import json
import sys
import zlib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd  # noqa: E402

import layers.diagnosis as diagnosis_module  # noqa: E402
import store  # noqa: E402
from config import settings  # noqa: E402
from db import Base, get_engine, get_session, init_db  # noqa: E402
from layers.detection import check_anomaly  # noqa: E402
from layers.ingestion import generate_events, split_treatment_control  # noqa: E402
from layers.pipeline import run_recovery  # noqa: E402
from layers.reconciliation import resolve_path  # noqa: E402
from models import ReconciliationLedger  # noqa: E402

# --- simulation constants; the headline figures move directly with these -----
#
# BASELINE self-healing. A control-arm decline is NOT permanently lost: transient
# failures resolve on their own when the bank recovers or the customer retries.
# Modelling control as zero recovery would make "Rupees Recovered = Treatment -
# Control" a restatement of Treatment rather than a measure of lift, which is
# precisely the fudging plan.md's verification table warns against.
#
# Grounded in the ontology instead of an invented rate: transient causes self-heal,
# sticky ones (revoked / paused / auth) do not. Deterministic, no random draw.
_SELF_HEALING_CAUSES = frozenset({"bank_downtime", "technical_decline"})

# Settlement does not land in the same instant as dispatch. A deterministic
# per-event delay keeps MTTR a real duration instead of ~0 seconds. Spread
# deliberately straddles the 24h SLA so the recovery-rate metric is non-trivial.
_MIN_SETTLE_MINUTES = 3
_MAX_SETTLE_MINUTES = 36 * 60


def _settle_delay(event_id: str) -> timedelta:
    """Stable pseudo-delay derived from the event id. Same id -> same delay.

    `zlib.crc32`, not `hash()`: Python randomises string hashing per process
    (PYTHONHASHSEED), so `hash()` would give a different MTTR on every run and
    quietly break the reproducibility this project is built on.
    """
    span = _MAX_SETTLE_MINUTES - _MIN_SETTLE_MINUTES
    digest = zlib.crc32(event_id.encode("utf-8"))
    return timedelta(minutes=_MIN_SETTLE_MINUTES + (digest % span))


# Offline Tier 2 stub: maps the dataset's ambiguous strings the way the real
# model does (verified by scripts/tier2_calibration.py), so seeding needs no key.
_STUB = {
    "BANK_NOT_AVAILABLE": ("bank_downtime", 0.95),
    "PER_TXN_LIMIT_EXCEEDED": ("payer_limit_exceeded", 0.95),
    "MANDATE_SUSPENDED": ("mandate_paused", 0.95),
    "MANDATE_REVOKED_BY_PAYER": ("mandate_revoked", 1.0),
    "AUTH_TIMEOUT": ("authentication_failure", 0.95),
    "TECHNICAL_ERROR": ("technical_decline", 0.95),
}


def _stub_llm(sanitized_error: str, *, model: str | None = None) -> str:
    cause, confidence = _STUB.get(sanitized_error.strip().upper(), ("unknown", 0.1))
    return json.dumps({"cause": cause, "confidence": confidence, "rationale": "offline stub"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="use the real Gemini Tier 2 call instead of the offline stub")
    parser.add_argument("--keep", action="store_true",
                        help="append to existing data instead of resetting the schema")
    args = parser.parse_args()

    if not args.live:
        diagnosis_module.call_tier2_llm = _stub_llm
        print("Tier 2: OFFLINE STUB (pass --live for real Gemini calls)")
    else:
        if not settings.google_api_key:
            print("GOOGLE_API_KEY is not set - cannot run --live.")
            return 1
        print(f"Tier 2: LIVE ({settings.tier2_model})")

    if not args.keep:
        Base.metadata.drop_all(bind=get_engine())
    init_db()

    df = generate_events(settings.dataset_n, settings.dataset_seed)
    treatment, _control = split_treatment_control(df, settings.dataset_seed)
    treatment_mandates = set(treatment["mandate_id"])
    df = df.copy()
    df["arm"] = df["mandate_id"].apply(
        lambda m: "treatment" if m in treatment_mandates else "control"
    )

    ts = pd.to_datetime(df["event_ts"]).dt.tz_localize(None)
    next_cycle = (ts.dt.to_period("M") + 1).dt.to_timestamp(how="start")
    df["days_to_next_cycle"] = (next_cycle - ts).dt.days.clip(lower=0)

    print(f"dataset: n={len(df)} seed={settings.dataset_seed}  "
          f"treatment={int((df['arm'] == 'treatment').sum())} "
          f"control={int((df['arm'] == 'control').sum())}")

    ingested = recovered = settled = skipped_control = control_healed = 0
    session = get_session()
    try:
        for row in df.itertuples():
            segment = f"{row.bank}:{row.mandate_type}"
            store.upsert_mandate(
                session,
                mandate_id=row.mandate_id,
                customer_id=row.customer_id,
                bank=row.bank,
                mandate_type=row.mandate_type,
                reliability_score=float(row.mandate_reliability),
            )
            is_new = store.insert_decline_event(
                session,
                event={
                    "event_id": row.event_id,
                    "mandate_id": row.mandate_id,
                    "billing_cycle": row.billing_cycle,
                    "segment": segment,
                    "bank": row.bank,
                    "mandate_type": row.mandate_type,
                    "event_ts": row.event_ts,
                    "amount": int(row.amount),
                    "raw_error_code": row.raw_error_code,
                    "arm": row.arm,
                },
            )
            if is_new:
                ingested += 1

            history, observed = store.segment_daily_history(
                session, segment=segment, as_of=row.event_ts.date()
            )
            store.insert_detected_anomaly(
                session,
                segment=segment,
                window_start=row.event_ts - timedelta(days=60),
                window_end=row.event_ts,
                result=check_anomaly(history, observed),
            )
            session.commit()

            # CONTROL ARM: diagnosed and detected like everything else, but no
            # recovery action fires. That absence is the counterfactual the
            # Rupees Recovered metric is measured against.
            if row.arm == "control":
                skipped_control += 1
                # Baseline counterfactual: transient failures still recover on
                # their own, with no intervention and no action row. This is what
                # the treatment arm is measured AGAINST.
                if row.true_cause in _SELF_HEALING_CAUSES:
                    resolve_path(
                        session,
                        mandate_id=row.mandate_id,
                        billing_cycle=row.billing_cycle,
                        path="mandate",
                        amount=int(row.amount),
                        now=datetime.now(timezone.utc) + _settle_delay(row.event_id),
                    )
                    session.commit()
                    control_healed += 1
                continue

            outcome = run_recovery(
                session,
                event_id=row.event_id,
                mandate_id=row.mandate_id,
                billing_cycle=row.billing_cycle,
                raw_error_code=row.raw_error_code,
                amount=int(row.amount),
                mandate_reliability=float(row.mandate_reliability),
                days_to_next_cycle=int(row.days_to_next_cycle),
            )
            session.commit()
            if outcome.fired:
                recovered += 1

            # Money only lands for actions that actually collect. RETRY and
            # NUDGE settle on the mandate rail; ALT_RAIL settles on its own.
            action = outcome.decision["action"]
            if action in ("RETRY", "NUDGE_BALANCE", "ALT_RAIL") and outcome.fired:
                path = "alt_rail" if action == "ALT_RAIL" else "mandate"
                result = resolve_path(
                    session,
                    mandate_id=row.mandate_id,
                    billing_cycle=row.billing_cycle,
                    path=path,
                    amount=int(row.amount),
                    now=datetime.now(timezone.utc) + _settle_delay(row.event_id),
                )
                session.commit()
                if result.status == "settled":
                    settled += 1

        # --- exercise the failure paths so Ops/Reconciliation are not empty ----
        # Every row below is produced by the REAL layer functions; nothing is
        # written directly. A demo with an empty Ops queue proves nothing about
        # the escalation paths, and a ledger with no collisions never shows the
        # double-charge guard doing its job.
        from sqlalchemy import select

        from layers.reconciliation import sweep_expired_holds
        from models import ActionsLog
        from tasks.ttl_watchdog import sweep_stuck_actions

        # 1. Collisions: the mandate rail also collects on keys that already
        #    settled. The partial unique index forces the second into a refund.
        settled_keys = session.execute(
            select(ReconciliationLedger.mandate_id, ReconciliationLedger.billing_cycle,
                   ReconciliationLedger.path, ReconciliationLedger.amount)
            .where(ReconciliationLedger.status == "settled")
            .limit(8)
        ).all()
        collisions = 0
        for m_id, b_cycle, path, amt in settled_keys:
            other = "mandate" if path == "alt_rail" else "alt_rail"
            res = resolve_path(
                session, mandate_id=m_id, billing_cycle=b_cycle, path=other, amount=amt,
                now=datetime.now(timezone.utc) + timedelta(minutes=4),
            )
            session.commit()
            if res.status == "auto_refunded":
                collisions += 1

        # 2. Stuck actions: backdate a few past the TTL, then run the real sweep.
        stuck = session.execute(
            select(ActionsLog).where(ActionsLog.resolved_at.is_(None)).limit(5)
        ).scalars().all()
        for a in stuck:
            a.created_at = datetime.now(timezone.utc) - timedelta(
                seconds=settings.ttl_processing_seconds + 3600
            )
        session.commit()
        ttl = sweep_stuck_actions(session)
        session.commit()

        # 3. Settlement holds that expired with nothing settled.
        expiry = sweep_expired_holds(session)
        session.commit()

        print(f"\ningested events    : {ingested}")
        print(f"collisions refunded: {collisions}")
        print(f"TTL escalations    : {ttl.escalated}")
        print(f"expired holds      : {expiry.escalated} escalated, {expiry.superseded} superseded")
        print(f"actions dispatched : {recovered}")
        print(f"paths settled      : {settled}")
        print(f"control (no action): {skipped_control}  (of which self-healed organically: {control_healed})")

        from layers.metrics import compute_metrics

        m = compute_metrics(session)
        print("\n--- Layer 6 summary ---")
        print("MTTR by tier      :", m.mttr_by_tier)
        print("Rupees recovered  :", m.recovered)
        print("recovery rate     :", m.recovery_rate)
        print("actions           :", m.actions_by_type)
        print("ledger            :", m.ledger_by_status)
        print("quarantine        :", m.quarantine["pending_ops_review"], "pending")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

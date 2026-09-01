"""FastAPI entry point for Avirata.

Phase 2 exposes the ingest -> detection path; Phase 4 adds the recovery
cascade (diagnosis -> policy -> idempotent dispatch -> comms) on its own route.
Reconciliation and dashboard routes are added in later phases.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import FastAPI
from pydantic import BaseModel, Field

import store
from db import get_session
from layers.detection import check_anomaly
from layers.pipeline import run_recovery

app = FastAPI(title="Avirata — Silent Mandate Death Recovery Agent")

_LOOKBACK_DAYS = 60


class DeclineEventIn(BaseModel):
    """One decline event as delivered by the mandate webhook.

    `raw_error_code` is the ONLY failure signal — there is no ground-truth
    cause on the wire, by construction.
    """

    event_id: str
    mandate_id: str
    customer_id: str
    bank: str
    mandate_type: str
    event_ts: datetime
    billing_cycle: str
    amount: int
    mandate_reliability: float = Field(default=0.9, ge=0.0, le=1.0)
    raw_error_code: str


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "avirata", "phase": "4-recovery-policy"}


@app.post("/api/events/ingest")
def ingest_event(event: DeclineEventIn) -> dict:
    """Persist a decline event, then run MAD detection on its segment.

    The response always carries a defined `status` (anomaly / normal /
    insufficient_data) plus the numbers behind it — never a bare boolean.
    """
    segment = f"{event.bank}:{event.mandate_type}"
    as_of = event.event_ts.date()

    with get_session() as session:
        store.upsert_mandate(
            session,
            mandate_id=event.mandate_id,
            customer_id=event.customer_id,
            bank=event.bank,
            mandate_type=event.mandate_type,
            reliability_score=event.mandate_reliability,
        )
        is_new = store.insert_decline_event(
            session,
            event={
                "event_id": event.event_id,
                "mandate_id": event.mandate_id,
                "billing_cycle": event.billing_cycle,
                "segment": segment,
                "bank": event.bank,
                "mandate_type": event.mandate_type,
                "event_ts": event.event_ts,
                "amount": event.amount,
                "raw_error_code": event.raw_error_code,
            },
        )

        history, observed = store.segment_daily_history(
            session, segment=segment, as_of=as_of, lookback_days=_LOOKBACK_DAYS
        )
        result = check_anomaly(history, observed)

        anomaly_id = store.insert_detected_anomaly(
            session,
            segment=segment,
            window_start=event.event_ts - timedelta(days=_LOOKBACK_DAYS),
            window_end=event.event_ts,
            result=result,
        )
        session.commit()

    return {
        "event_id": event.event_id,
        "duplicate": not is_new,
        "segment": segment,
        "detection_id": anomaly_id,
        "detection": result.to_dict(),
    }


class RecoveryRequestIn(BaseModel):
    """Ask for one event to be diagnosed and recovered.

    Kept separate from `/api/events/ingest` on purpose: ingestion is a
    high-volume webhook that must stay fast and never call an LLM, whereas
    recovery is the deliberate act of spending money on a failure.
    """

    event_id: str
    mandate_id: str
    billing_cycle: str
    raw_error_code: str
    amount: int
    mandate_reliability: float = Field(default=0.9, ge=0.0, le=1.0)
    days_to_next_cycle: int = Field(default=15, ge=0)


@app.post("/api/events/recover")
def recover_event(req: RecoveryRequestIn) -> dict:
    """Run the Phase 4 cascade for one event and dispatch at most one action.

    Safe to replay: the `(mandate_id, billing_cycle)` UNIQUE constraint means a
    duplicate call returns `fired: false` and sends no further messages.
    """
    with get_session() as session:
        outcome = run_recovery(
            session,
            event_id=req.event_id,
            mandate_id=req.mandate_id,
            billing_cycle=req.billing_cycle,
            raw_error_code=req.raw_error_code,
            amount=req.amount,
            mandate_reliability=req.mandate_reliability,
            days_to_next_cycle=req.days_to_next_cycle,
        )
        session.commit()
    return outcome.to_dict()

"""FastAPI entry point for Avirata.

Phase 2 exposes the ingest -> detection path; Phase 4 adds the recovery
cascade (diagnosis -> policy -> idempotent dispatch -> comms) on its own route.
Reconciliation and dashboard routes are added in later phases.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import store
from db import get_session
from layers.detection import check_anomaly
from layers.pipeline import run_recovery
from layers.diagnosis import (
    ONTOLOGY_SET,
    OntologyPromotionError,
    TIER1_RULES,
    promote_to_tier1,
)
from layers.metrics import compute_metrics, decision_trace
from layers.reconciliation import resolve_path

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
    arm: Literal["treatment", "control"] | None = None


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "avirata", "phase": "6-metrics-dashboard"}


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
                "arm": event.arm,
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


class SettlementWebhookIn(BaseModel):
    """A payment rail reporting that it collected for one billing cycle.

    Either rail can report first. Whichever does wins; a second arrival for the
    same key is a collision and gets refunded rather than settled.
    """

    mandate_id: str
    billing_cycle: str
    path: Literal["mandate", "alt_rail"]
    amount: int | None = None


@app.post("/api/webhooks/settlement")
def settlement_webhook(req: SettlementWebhookIn) -> dict:
    """Record a settlement, auto-refunding a collision inside the hold window.

    Safe to replay: a webhook for an already-terminal path is reported back as
    that same terminal status and changes nothing.
    """
    with get_session() as session:
        result = resolve_path(
            session,
            mandate_id=req.mandate_id,
            billing_cycle=req.billing_cycle,
            path=req.path,
            amount=req.amount,
        )
        session.commit()
    return {
        "mandate_id": req.mandate_id,
        "billing_cycle": req.billing_cycle,
        **result.to_dict(),
    }


@app.get("/api/dashboard/summary")
def dashboard_summary() -> dict:
    """Every Layer 6 metric in one call, computed from stored rows only."""
    with get_session() as session:
        return compute_metrics(session).to_dict()


@app.get("/api/audit/decision/{mandate_id}/{billing_cycle}")
def audit_decision(mandate_id: str, billing_cycle: str) -> dict:
    """Full decision trace for one key: event -> diagnosis -> action -> ledger -> Ops.

    `found: false` with empty lists is a real answer (nothing ever happened for
    this key), deliberately not a 404 — an auditor asking about a key that was
    never touched deserves that stated, not an error page.
    """
    with get_session() as session:
        return decision_trace(
            session, mandate_id=mandate_id, billing_cycle=billing_cycle
        )


_DIST = Path(__file__).parent / "static" / "dist"
_LEGACY_DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"

# Hashed JS/CSS from `npm run build`. Mounted only when the bundle exists so the
# API still starts on a machine that has never run the frontend build.
if (_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the React console, falling back to the pre-React single-file page.

    The fallback keeps `uvicorn app:app` working before anyone has run
    `npm install && npm run build`, rather than returning a 404 that looks like
    a broken backend.
    """
    index = _DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return FileResponse(_LEGACY_DASHBOARD)


class OntologyPromotionIn(BaseModel):
    """Ops approving a quarantined string into the Tier 1 rule dict."""

    raw_input: str
    target_cause: str


@app.post("/api/ontology/promote")
def promote_ontology(req: OntologyPromotionIn) -> dict:
    """Promote a quarantined decline string to a Tier 1 rule.

    Closes the ontology loop: a string that fell to Tier 3 is reviewed, mapped,
    and thereafter resolves instantly at Tier 1 without an LLM call.

    DEMO SCOPE: the rule lives in the process's `TIER1_RULES` dict and does not
    survive a restart. Production would persist it with an approver and an audit
    record; nothing here records WHO approved the mapping.
    """
    try:
        result = promote_to_tier1(req.raw_input, req.target_cause)
    except OntologyPromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "rules_count": result["rules_count"],
        "added": {
            "raw_input": result["key"],
            "target_cause": result["target_cause"],
        },
        "replaced": result["previous_cause"],
        "note": "in-memory for the demo; a restart reverts it",
    }


@app.get("/api/ontology/rules")
def ontology_rules() -> dict:
    """Current Tier 1 rule dict and the ontology it maps into.

    Lets the UI show the promotion actually landing, rather than asking the
    operator to take the POST response on trust.
    """
    return {
        "rules": dict(sorted(TIER1_RULES.items())),
        "rules_count": len(TIER1_RULES),
        "ontology": sorted(ONTOLOGY_SET),
    }

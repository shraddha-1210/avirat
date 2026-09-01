"""ORM schema for the Avirata pipeline.

Idempotency is enforced at the DATABASE level: `actions_log` carries a UNIQUE
constraint on (mandate_id, billing_cycle) and Layer 4c writes with
`INSERT ... ON CONFLICT DO NOTHING`. Application locks are NOT used.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class Mandate(Base):
    __tablename__ = "mandates"

    mandate_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(32), index=True)
    bank: Mapped[str] = mapped_column(String(16), index=True)
    mandate_type: Mapped[str] = mapped_column(String(24))
    reliability_score: Mapped[float] = mapped_column(Float, default=0.9)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    events: Mapped[list["DeclineEvent"]] = relationship(back_populates="mandate")


class DeclineEvent(Base):
    __tablename__ = "decline_events"

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    mandate_id: Mapped[str] = mapped_column(ForeignKey("mandates.mandate_id"), index=True)
    billing_cycle: Mapped[str] = mapped_column(String(7), index=True)  # 'YYYY-MM'
    segment: Mapped[str] = mapped_column(String(48), index=True)       # 'BANK:MANDATE_TYPE'
    bank: Mapped[str] = mapped_column(String(16))
    mandate_type: Mapped[str] = mapped_column(String(24))
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    amount: Mapped[int] = mapped_column(Integer)  # rupees
    raw_error_code: Mapped[str] = mapped_column(String(255))  # the ONLY signal downstream layers see
    arm: Mapped[str | None] = mapped_column(String(12), nullable=True)  # 'treatment' | 'control'
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    mandate: Mapped["Mandate"] = relationship(back_populates="events")


class DetectedAnomaly(Base):
    __tablename__ = "detected_anomalies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    segment: Mapped[str] = mapped_column(String(48), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sample_size: Mapped[int] = mapped_column(Integer)
    median: Mapped[float | None] = mapped_column(Float, nullable=True)
    mad: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_value: Mapped[float] = mapped_column(Float)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24))  # 'anomaly' | 'normal' | 'insufficient_data'
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("decline_events.event_id"), index=True)
    tier: Mapped[int] = mapped_column(Integer)  # 1 | 2 | 3
    cause: Mapped[str | None] = mapped_column(String(48), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24))  # 'resolved' | 'QUARANTINE'
    raw_input: Mapped[str] = mapped_column(Text)
    sanitized_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActionsLog(Base):
    __tablename__ = "actions_log"
    __table_args__ = (
        UniqueConstraint("mandate_id", "billing_cycle", name="uq_actions_mandate_cycle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[str] = mapped_column(String(32), index=True)
    billing_cycle: Mapped[str] = mapped_column(String(7))
    action_type: Mapped[str] = mapped_column(String(24))  # RETRY | NUDGE_BALANCE | ALT_RAIL | SAFE_HOLD
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="processing")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReconciliationLedger(Base):
    __tablename__ = "reconciliation_ledger"
    __table_args__ = (
        UniqueConstraint("mandate_id", "billing_cycle", "path", name="uq_recon_key_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[str] = mapped_column(String(32), index=True)
    billing_cycle: Mapped[str] = mapped_column(String(7))
    path: Mapped[str] = mapped_column(String(12))  # 'mandate' | 'alt_rail'
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # 'pending' | 'settled' | 'auto_refunded' | 'expired_escalated'
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuarantineQueue(Base):
    __tablename__ = "quarantine_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(40), index=True)
    raw_input: Mapped[str] = mapped_column(Text)
    tier_attempted: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending_ops_review")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OpsEscalationQueue(Base):
    __tablename__ = "ops_escalation_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[str] = mapped_column(String(32), index=True)
    billing_cycle: Mapped[str] = mapped_column(String(7))
    reason: Mapped[str] = mapped_column(String(64))
    source_layer: Mapped[str] = mapped_column(String(24))  # 'ttl_watchdog' | 'reconciliation' | 'diagnosis'
    status: Mapped[str] = mapped_column(String(24), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommunicationState(Base):
    """Single owner of all outbound messaging per (mandate_id, billing_cycle)."""

    __tablename__ = "communication_state"
    __table_args__ = (
        UniqueConstraint("mandate_id", "billing_cycle", name="uq_comms_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[str] = mapped_column(String(32), index=True)
    billing_cycle: Mapped[str] = mapped_column(String(7))
    alt_rail_live: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

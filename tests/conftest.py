"""Shared test fixtures.

Rule (avirata_agent_constraints.md): the Tier 2 LLM call is mocked in EVERY
automated test — CI never hits the real Gemini API. Only the live/demo path
uses a real key. `layers.diagnosis.call_tier2_llm` is the single network seam;
everything below patches it.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import layers.diagnosis as diagnosis


@pytest.fixture(autouse=True)
def _clear_tier2_cache():
    """The Tier 2 memo is process-global; never let it leak across tests."""
    diagnosis.clear_tier2_cache()
    yield
    diagnosis.clear_tier2_cache()


@pytest.fixture(autouse=True)
def _no_real_api_calls(monkeypatch):
    """Hard stop: any un-patched Tier 2 call fails loudly instead of hitting the network."""

    def _explode(*_args, **_kwargs):
        raise AssertionError(
            "a test reached the real Gemini API; patch call_tier2_llm instead"
        )

    monkeypatch.setattr(diagnosis, "call_tier2_llm", _explode)


@pytest.fixture
def mock_llm_call(monkeypatch):
    """Patched Tier 2 seam returning a valid, high-confidence JSON reply.

    Set `mock_llm_call.return_value` to any raw string to simulate a malformed
    or low-confidence reply. Call assertions (`assert_not_called`, `call_args`)
    work as normal.
    """
    mock = MagicMock(
        return_value=json.dumps(
            {"cause": "insufficient_funds", "confidence": 0.93, "rationale": "low balance"}
        )
    )
    monkeypatch.setattr(diagnosis, "call_tier2_llm", mock)
    return mock


# ---------------------------------------------------------------------------
# Phase 4 — real PostgreSQL fixtures.
#
# The idempotency burst test is only meaningful against a real UNIQUE
# constraint (testing.md Phase 4, Test 2): SQLite or a mocked lock would prove
# nothing. These fixtures therefore refuse to substitute a fake — if Postgres
# is unreachable the test SKIPS loudly rather than passing vacuously.
#
#   docker compose up -d db
# ---------------------------------------------------------------------------
_PG_TABLES = (
    "ops_escalation_queue",
    "communication_state",
    "actions_log",
    "reconciliation_ledger",
    "quarantine_queue",
    "diagnoses",
    "detected_anomalies",
    "decline_events",
    "mandates",
)


@pytest.fixture(scope="session")
def pg_engine():
    """Real Postgres engine, or skip. Never falls back to another dialect."""
    from sqlalchemy import text

    from db import get_engine, init_db

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"PostgreSQL unreachable ({type(exc).__name__}) — run `docker compose up -d db`. "
            "This suite refuses to fake the UNIQUE constraint."
        )

    if engine.dialect.name != "postgresql":
        pytest.skip(f"dialect is {engine.dialect.name!r}, not postgresql — constraint test invalid")

    init_db()
    return engine


@pytest.fixture
def pg_session(pg_engine):
    """Clean session per test; every table truncated before and after."""
    from sqlalchemy import text

    from db import get_session

    def _truncate():
        with pg_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {', '.join(_PG_TABLES)} RESTART IDENTITY CASCADE"))

    _truncate()
    session = get_session()
    try:
        yield session
    finally:
        session.close()
        _truncate()

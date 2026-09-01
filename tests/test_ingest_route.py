"""Phase 2 — /api/events/ingest route wiring.

The route's DB layer is patched here so the test needs no network and no
Postgres. The real `store.py` writes are exercised against a live Postgres in
Phase 4, alongside the idempotency burst test — a green result here proves the
route calls detection correctly and shapes its response, nothing more.
"""
from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

import app as app_module


class _StubSession:
    """Minimal stand-in for a SQLAlchemy Session."""

    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:  # pragma: no cover
        pass

    def close(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def client(monkeypatch):
    calls: dict = {}

    @contextlib.contextmanager
    def fake_session():
        session = _StubSession()
        calls["session"] = session
        yield session

    def fake_persist(session, **kw):
        calls["persisted"] = kw
        return 1

    monkeypatch.setattr(app_module, "get_session", fake_session)
    monkeypatch.setattr(app_module.store, "upsert_mandate", lambda s, **kw: None)
    monkeypatch.setattr(app_module.store, "insert_decline_event", lambda s, **kw: True)
    monkeypatch.setattr(app_module.store, "insert_detected_anomaly", fake_persist)
    monkeypatch.setattr(
        app_module.store,
        "segment_daily_history",
        lambda s, **kw: ([1.0] * 20 + [2.0] * 25, 30.0),  # n=45, median 2 -> spike
    )
    c = TestClient(app_module.app)
    c.calls = calls  # type: ignore[attr-defined]
    return c


PAYLOAD = {
    "event_id": "EVT-TEST-1",
    "mandate_id": "MND-00001",
    "customer_id": "CUST-00001",
    "bank": "ICICI",
    "mandate_type": "UPI_AUTOPAY",
    "event_ts": "2026-08-31T10:00:00+00:00",
    "billing_cycle": "2026-08",
    "amount": 999,
    "mandate_reliability": 0.8,
    "raw_error_code": "U30",
}


def test_health():
    with TestClient(app_module.app) as c:
        assert c.get("/api/health").json()["status"] == "ok"


def test_ingest_returns_detection_with_numbers(client):
    body = client.post("/api/events/ingest", json=PAYLOAD).json()

    assert body["segment"] == "ICICI:UPI_AUTOPAY"
    assert body["duplicate"] is False
    detection = body["detection"]
    assert detection["status"] == "anomaly"
    assert detection["is_anomaly"] is True
    # numbers travel with the flag, never a bare boolean
    assert detection["sample_size"] == 45
    assert detection["median"] is not None
    assert detection["mad"] is not None
    assert detection["threshold"] is not None
    assert detection["deviation"] > detection["threshold"]


def test_ingest_persists_the_detection_and_commits(client):
    client.post("/api/events/ingest", json=PAYLOAD)
    persisted = client.calls["persisted"]  # type: ignore[attr-defined]
    assert persisted["segment"] == "ICICI:UPI_AUTOPAY"
    assert persisted["result"].is_anomaly is True
    assert client.calls["session"].committed is True  # type: ignore[attr-defined]


def test_sparse_segment_returns_insufficient_data(client, monkeypatch):
    monkeypatch.setattr(
        app_module.store, "segment_daily_history", lambda s, **kw: ([1.0] * 29, 500.0)
    )
    detection = client.post("/api/events/ingest", json=PAYLOAD).json()["detection"]
    assert detection["status"] == "insufficient_data"
    assert detection["is_anomaly"] is False
    assert detection["mad"] is None


def test_ingest_flags_a_replayed_event_as_duplicate(client, monkeypatch):
    monkeypatch.setattr(app_module.store, "insert_decline_event", lambda s, **kw: False)
    assert client.post("/api/events/ingest", json=PAYLOAD).json()["duplicate"] is True

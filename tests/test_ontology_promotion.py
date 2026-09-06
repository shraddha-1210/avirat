"""Ontology loop — Tier 3 quarantine -> Ops approval -> persisted Tier 1 rule.

Two kinds of state have to be isolated here, and missing either one silently
corrupts other tests:

* `TIER1_RULES` is process-global, so `restore_rules` snapshots and restores it.
* `tier1_promoted_rules` is now a real table, so `clean_promoted_table` truncates
  it around every test. Without that, a promotion in one test would be reloaded by
  a later `load_promoted_rules()` and change diagnosis behaviour across the suite.

Promotion writes to Postgres before it touches memory, so these tests need a real
database. They SKIP loudly when it is unreachable rather than passing vacuously,
the same doctrine as the idempotency and reconciliation suites.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import app as app_module
import layers.diagnosis as diagnosis
from layers.diagnosis import (
    ONTOLOGY_SET,
    TIER1_RULES,
    OntologyPersistenceError,
    OntologyPromotionError,
    diagnose,
    load_promoted_rules,
    promote_to_tier1,
)

NOVEL = "XZ-991"


@pytest.fixture(autouse=True)
def restore_rules():
    """The rule dict is global; never let one test's promotion leak into another."""
    snapshot = dict(TIER1_RULES)
    yield
    TIER1_RULES.clear()
    TIER1_RULES.update(snapshot)


@pytest.fixture(autouse=True)
def clean_promoted_table(pg_engine):
    """Empty `tier1_promoted_rules` around each test. Requires real Postgres.

    Depends on `pg_engine` so an unreachable database skips this module loudly
    instead of failing in a way that looks like a promotion bug.
    """
    def _truncate():
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE tier1_promoted_rules RESTART IDENTITY"))

    _truncate()
    yield
    _truncate()


@pytest.fixture
def client():
    return TestClient(app_module.app)


def _rows(pg_engine) -> list[tuple]:
    with pg_engine.begin() as conn:
        return conn.execute(
            text("SELECT raw_input, target_cause, promoted_by FROM tier1_promoted_rules")
        ).all()


# ---------------------------------------------------------------------------
# the loop itself
# ---------------------------------------------------------------------------
def test_promotion_moves_a_string_from_quarantine_to_tier1(mock_llm_call):
    """The headline behaviour: quarantined today, resolved instantly tomorrow."""
    mock_llm_call.return_value = '{"cause": "unknown", "confidence": 0.1, "rationale": "no match"}'
    before = diagnose(NOVEL, use_cache=False)
    assert before.tier == 3 and before.status == "QUARANTINE"

    promote_to_tier1(NOVEL, "bank_downtime")

    after = diagnose(NOVEL, use_cache=False)
    assert after.tier == 1
    assert after.status == "resolved"
    assert after.cause == "bank_downtime"
    assert after.confidence == 1.0


def test_promoted_string_never_reaches_the_llm_again(mock_llm_call):
    """Tier 1 short-circuits before any network call — that is the point."""
    promote_to_tier1(NOVEL, "bank_downtime")
    diagnose(NOVEL, use_cache=False)
    mock_llm_call.assert_not_called()


def test_key_is_normalised_the_way_tier1_looks_it_up():
    """A rule stored un-normalised would never match and the loop would silently
    not close — the worst kind of failure, since the UI would report success."""
    promote_to_tier1("  err_unmapped_9007  ", "technical_decline")
    assert TIER1_RULES["ERR_UNMAPPED_9007"] == "technical_decline"
    assert diagnose("err_unmapped_9007").cause == "technical_decline"
    assert diagnose("  ERR_UNMAPPED_9007 ").cause == "technical_decline"


def test_promotion_reports_what_it_replaced():
    """Re-mapping an existing code must be visible, not silent."""
    result = promote_to_tier1("U30", "technical_decline")
    assert result["previous_cause"] == "bank_downtime"
    assert result["target_cause"] == "technical_decline"


def test_rules_count_grows_by_exactly_one():
    before = len(TIER1_RULES)
    result = promote_to_tier1(NOVEL, "bank_downtime")
    assert result["rules_count"] == before + 1


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
def test_promotion_survives_a_process_restart(pg_engine):
    """THE persistence proof: drop the in-memory rule the way a restart would,
    reload from the table, and the promoted rule is still live.

    `load_promoted_rules()` is exactly what application startup calls, so this
    exercises the real reload path rather than a test-only shortcut.
    """
    promote_to_tier1(NOVEL, "mandate_paused")
    assert TIER1_RULES[NOVEL] == "mandate_paused"

    # Simulate the restart: nothing survives in memory.
    TIER1_RULES.clear()
    TIER1_RULES.update(diagnosis._BASELINE_TIER1_RULES)
    assert NOVEL not in TIER1_RULES

    loaded = load_promoted_rules()

    assert loaded == 1
    assert TIER1_RULES[NOVEL] == "mandate_paused"
    after = diagnose(NOVEL, use_cache=False)
    assert after.tier == 1 and after.confidence == 1.0 and after.cause == "mandate_paused"


def test_reload_is_idempotent_and_does_not_accumulate():
    """Calling the startup reload twice must not double-count or strand old keys."""
    promote_to_tier1(NOVEL, "bank_downtime")
    first = load_promoted_rules()
    size_after_first = len(TIER1_RULES)
    second = load_promoted_rules()

    assert first == second == 1
    assert len(TIER1_RULES) == size_after_first


def test_reload_rebuilds_from_baseline_so_a_deleted_row_stops_applying(pg_engine):
    """A rule removed from the table must not linger in the dict after a reload."""
    promote_to_tier1(NOVEL, "bank_downtime")
    load_promoted_rules()
    assert NOVEL in TIER1_RULES

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM tier1_promoted_rules"))

    load_promoted_rules()
    assert NOVEL not in TIER1_RULES


def test_promoting_twice_updates_instead_of_duplicating(pg_engine):
    """Second promote of the same key is an UPDATE, not a second row."""
    promote_to_tier1(NOVEL, "bank_downtime")
    result = promote_to_tier1(NOVEL, "mandate_revoked")

    rows = _rows(pg_engine)
    assert len(rows) == 1, f"expected one row per key, got {rows}"
    assert rows[0] == (NOVEL, "mandate_revoked", "ops")
    assert result["previous_cause"] == "bank_downtime"
    assert TIER1_RULES[NOVEL] == "mandate_revoked"


def test_promoted_at_advances_on_re_promotion(pg_engine):
    """The timestamp has to move, or the audit trail records the wrong approval."""
    promote_to_tier1(NOVEL, "bank_downtime")
    with pg_engine.begin() as conn:
        first = conn.execute(text("SELECT promoted_at FROM tier1_promoted_rules")).scalar_one()

    promote_to_tier1(NOVEL, "mandate_revoked")
    with pg_engine.begin() as conn:
        second = conn.execute(text("SELECT promoted_at FROM tier1_promoted_rules")).scalar_one()

    assert second >= first


def test_promotion_records_who_approved_it(pg_engine):
    promote_to_tier1(NOVEL, "bank_downtime")
    assert _rows(pg_engine)[0][2] == "ops"


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------
def _failing_session() -> MagicMock:
    """A session whose write blows up the way an unreachable database would."""
    session = MagicMock()
    session.execute.side_effect = OperationalError("INSERT", {}, Exception("no connection"))
    return session


def test_db_failure_does_not_update_memory(pg_engine):
    """Fail closed. A rule the process honours but cannot reproduce after a restart
    is exactly the divergence persistence exists to remove."""
    before = dict(TIER1_RULES)
    session = _failing_session()

    with pytest.raises(OntologyPersistenceError):
        promote_to_tier1(NOVEL, "bank_downtime", session=session)

    assert NOVEL not in TIER1_RULES
    assert TIER1_RULES == before
    assert _rows(pg_engine) == []
    session.rollback.assert_called_once()


def test_db_failure_returns_500_not_400(client, monkeypatch):
    """500 and 400 must stay distinguishable: one means our storage is down, the
    other means the operator asked for something invalid."""
    import db as db_module

    monkeypatch.setattr(db_module, "get_session", _failing_session)

    res = client.post(
        "/api/ontology/promote",
        json={"raw_input": NOVEL, "target_cause": "bank_downtime"},
    )

    assert res.status_code == 500
    assert NOVEL not in TIER1_RULES


def test_validation_failure_writes_nothing(pg_engine):
    """A refused promotion must not leave a row behind."""
    with pytest.raises(OntologyPromotionError):
        promote_to_tier1(NOVEL, "totally_made_up")
    assert _rows(pg_engine) == []


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def test_off_ontology_cause_is_refused():
    with pytest.raises(OntologyPromotionError, match="not in the ontology"):
        promote_to_tier1(NOVEL, "totally_made_up")
    assert NOVEL not in TIER1_RULES


def test_promoting_to_unknown_is_refused():
    """A Tier 1 rule resolves with confidence 1.0, so mapping to 'unknown' would
    assert we confidently know it is unknown — and would bypass quarantine."""
    assert "unknown" in ONTOLOGY_SET, "guard assumes 'unknown' is in the ontology"
    with pytest.raises(OntologyPromotionError, match="non-resolving"):
        promote_to_tier1(NOVEL, "unknown")
    assert NOVEL not in TIER1_RULES


def test_empty_raw_input_is_refused():
    with pytest.raises(OntologyPromotionError, match="must not be empty"):
        promote_to_tier1("   ", "bank_downtime")


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
def test_endpoint_promotes_and_returns_the_contract(client):
    res = client.post(
        "/api/ontology/promote",
        json={"raw_input": NOVEL, "target_cause": "bank_downtime"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["added"] == {"raw_input": NOVEL, "target_cause": "bank_downtime"}
    assert body["rules_count"] == len(TIER1_RULES)
    assert body["replaced"] is None


def test_endpoint_rejects_off_ontology_cause_with_400(client):
    res = client.post(
        "/api/ontology/promote",
        json={"raw_input": NOVEL, "target_cause": "not_a_cause"},
    )
    assert res.status_code == 400
    assert "not in the ontology" in res.json()["detail"]


def test_endpoint_rejects_unknown_with_400(client):
    res = client.post(
        "/api/ontology/promote", json={"raw_input": NOVEL, "target_cause": "unknown"}
    )
    assert res.status_code == 400


def test_rules_endpoint_reflects_the_promotion(client):
    client.post(
        "/api/ontology/promote",
        json={"raw_input": NOVEL, "target_cause": "mandate_paused"},
    )
    rules = client.get("/api/ontology/rules").json()
    assert rules["rules"][NOVEL] == "mandate_paused"
    assert rules["rules_count"] == len(TIER1_RULES)
    assert "unknown" in rules["ontology"]


def test_promotion_is_persisted_and_advertised_as_such(client):
    """The endpoint's own note is what an operator reads; it must not still claim
    the promotion is lost on restart now that it is not."""
    body = client.post(
        "/api/ontology/promote",
        json={"raw_input": NOVEL, "target_cause": "bank_downtime"},
    ).json()
    assert "persisted" in body["note"]
    assert "restart" in body["note"]
    assert diagnosis.TIER1_RULES[NOVEL] == "bank_downtime"


def test_promoted_rules_endpoint_lists_persisted_promotions(client):
    client.post(
        "/api/ontology/promote",
        json={"raw_input": NOVEL, "target_cause": "mandate_paused"},
    )
    body = client.get("/api/ontology/promoted-rules").json()

    assert body["count"] == 1
    row = body["promoted_rules"][0]
    assert row["raw_input"] == NOVEL
    assert row["target_cause"] == "mandate_paused"
    assert row["promoted_by"] == "ops"
    assert row["promoted_at"]  # ISO timestamp, present


def test_promoted_rules_endpoint_is_empty_when_nothing_promoted(client):
    body = client.get("/api/ontology/promoted-rules").json()
    assert body == {"promoted_rules": [], "count": 0}


def test_promoted_rules_endpoint_excludes_baseline_rules(client):
    """Baseline rules are not promotions; listing them would misreport what is durable."""
    body = client.get("/api/ontology/promoted-rules").json()
    assert body["count"] == 0
    rules = client.get("/api/ontology/rules").json()
    assert rules["rules_count"] >= len(diagnosis._BASELINE_TIER1_RULES)

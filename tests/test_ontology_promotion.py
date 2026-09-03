"""Ontology loop — Tier 3 quarantine -> Ops approval -> Tier 1 rule.

`promote_to_tier1` mutates the process-global `TIER1_RULES`, so every test here
takes `restore_rules`, which snapshots and restores it. Without that a promotion
in one test would silently change diagnosis behaviour in every test that runs
after it — including the Phase 3 suite.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
import layers.diagnosis as diagnosis
from layers.diagnosis import (
    ONTOLOGY_SET,
    TIER1_RULES,
    OntologyPromotionError,
    diagnose,
    promote_to_tier1,
)


@pytest.fixture(autouse=True)
def restore_rules():
    """The rule dict is global; never let one test's promotion leak into another."""
    snapshot = dict(TIER1_RULES)
    yield
    TIER1_RULES.clear()
    TIER1_RULES.update(snapshot)


@pytest.fixture
def client():
    return TestClient(app_module.app)


NOVEL = "XZ-991"


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


def test_promotion_survives_within_the_process_but_is_not_persisted(client):
    """Documents the demo limitation the endpoint's own response advertises."""
    body = client.post(
        "/api/ontology/promote",
        json={"raw_input": NOVEL, "target_cause": "bank_downtime"},
    ).json()
    assert "restart" in body["note"]
    assert diagnosis.TIER1_RULES[NOVEL] == "bank_downtime"

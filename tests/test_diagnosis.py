"""Phase 3 — Diagnosis tests (from testing.md Phase 3).

Test 1: Tier 1 bypasses the LLM entirely.
Test 2: input sanitization strips HTML/SQL before prompt assembly.
Test 3: malformed LLM JSON routes to Tier 3, never crashes.
Plus confidence gating, off-ontology rejection, and LLM-outage handling.
"""
from __future__ import annotations

import json

import pytest

from layers.diagnosis import (
    TIER1_RULES,
    diagnose,
    diagnose_batch,
    diagnose_tier1,
    diagnose_tier2,
    sanitize_input,
    tier_summary,
)
from layers.ingestion import ONTOLOGY_SET, generate_events


# --- testing.md Phase 3, Test 1: Tier 1 bypasses the LLM ----------------------
def test_tier1_known_code_never_calls_the_llm(mock_llm_call):
    result = diagnose("INSUFFICIENT_FUNDS")

    mock_llm_call.assert_not_called()
    assert result.tier == 1
    assert result.cause == "insufficient_funds"
    assert result.status == "resolved"
    assert result.confidence == 1.0


def test_tier1_dict_is_a_real_rule_set_within_the_ontology():
    assert len(TIER1_RULES) >= 8  # 8-10 known UPI decline codes
    assert set(TIER1_RULES.values()) <= ONTOLOGY_SET


def test_tier1_lookup_is_case_and_whitespace_insensitive():
    assert diagnose_tier1("  u14  ").cause == "insufficient_funds"


def test_tier1_returns_none_for_an_unknown_code():
    assert diagnose_tier1("BANK_NOT_AVAILABLE") is None


# --- testing.md Phase 3, Test 2: sanitization before prompt assembly ---------
def test_sanitization_strips_html_and_sql_before_the_prompt(mock_llm_call):
    raw = "<script>alert(1)</script>'; DROP TABLE mandates;--"
    sanitized = sanitize_input(raw)

    assert not any(c in sanitized for c in ["<", ">", ";", "--", "'"])

    diagnose_tier2(raw)
    # the string actually handed to the LLM is the sanitized one, not the raw input
    assert mock_llm_call.call_args[0][0] == sanitized
    assert raw not in mock_llm_call.call_args[0][0]


def test_sanitization_strips_control_chars_and_caps_length():
    raw = "bank\x00said\x1fno " + "A" * 500
    sanitized = sanitize_input(raw)
    assert "\x00" not in sanitized and "\x1f" not in sanitized
    assert len(sanitized) <= 200


def test_sanitization_cannot_reform_a_sql_comment():
    # removed characters become spaces, so '-;-' must not collapse into '--'
    assert "--" not in sanitize_input("-;-")


# --- testing.md Phase 3, Test 3: malformed JSON -> Tier 3, no crash ---------
def test_malformed_llm_json_routes_to_tier3_without_raising(mock_llm_call):
    mock_llm_call.return_value = "not valid json {{{"

    result = diagnose_tier2("BANK_NOT_AVAILABLE")  # must not raise

    assert result.tier == 3
    assert result.status == "QUARANTINE"
    assert result.cause is None


def test_schema_violation_routes_to_tier3(mock_llm_call):
    # valid JSON, wrong shape (confidence out of range)
    mock_llm_call.return_value = json.dumps({"cause": "bank_downtime", "confidence": 7.5})
    result = diagnose_tier2("BANK_NOT_AVAILABLE")
    assert result.tier == 3
    assert result.status == "QUARANTINE"


def test_off_ontology_cause_is_rejected(mock_llm_call):
    mock_llm_call.return_value = json.dumps(
        {"cause": "customer_vibes_were_off", "confidence": 0.99}
    )
    result = diagnose_tier2("BANK_NOT_AVAILABLE")
    assert result.tier == 3
    assert result.cause is None


def test_llm_outage_is_quarantined_not_crashed(mock_llm_call):
    mock_llm_call.side_effect = RuntimeError("connection reset")
    result = diagnose_tier2("BANK_NOT_AVAILABLE")  # must not raise
    assert result.tier == 3
    assert result.status == "QUARANTINE"


# --- Tier 2 happy path + confidence gate -------------------------------------
def test_tier2_resolves_an_ambiguous_string_above_threshold(mock_llm_call):
    mock_llm_call.return_value = json.dumps({"cause": "bank_downtime", "confidence": 0.92})
    result = diagnose("BANK_NOT_AVAILABLE")

    mock_llm_call.assert_called_once()
    assert result.tier == 2
    assert result.cause == "bank_downtime"
    assert result.status == "resolved"
    assert result.confidence == pytest.approx(0.92)
    assert result.llm_model  # the deciding model is recorded for the audit trail


def test_confidence_below_threshold_is_quarantined(mock_llm_call):
    mock_llm_call.return_value = json.dumps({"cause": "bank_downtime", "confidence": 0.60})
    result = diagnose("BANK_NOT_AVAILABLE")
    assert result.tier == 3
    assert result.status == "QUARANTINE"
    assert result.confidence == pytest.approx(0.60)  # the number is kept for Ops


def test_llm_answering_unknown_is_not_treated_as_a_resolution(mock_llm_call):
    mock_llm_call.return_value = json.dumps({"cause": "unknown", "confidence": 0.99})
    result = diagnose("gateway declined: reason unclear")
    assert result.tier == 3
    assert result.cause is None


def test_tier2_memo_avoids_repeat_calls_for_the_same_string(mock_llm_call):
    mock_llm_call.return_value = json.dumps({"cause": "bank_downtime", "confidence": 0.92})
    for _ in range(5):
        diagnose("BANK_NOT_AVAILABLE")
    mock_llm_call.assert_called_once()


# --- whole-dataset cascade ----------------------------------------------------
def test_every_event_terminates_in_a_defined_state(mock_llm_call):
    # A realistic stand-in classifier: it maps the verbose-but-recognisable
    # variants, and honestly answers `unknown` on the novel strings — which is
    # what routes them to Tier 3.
    plausible = {
        "BANK_NOT_AVAILABLE": "bank_downtime",
        "MANDATE_REVOKED_BY_PAYER": "mandate_revoked",
        "MANDATE_SUSPENDED": "mandate_paused",
        "PER_TXN_LIMIT_EXCEEDED": "payer_limit_exceeded",
        "AUTH_TIMEOUT": "authentication_failure",
        "TECHNICAL_ERROR": "technical_decline",
    }

    def fake_classifier(sanitized: str, **_kwargs) -> str:
        cause = plausible.get(sanitized.strip().upper())
        if cause is None:
            return json.dumps({"cause": "unknown", "confidence": 0.2})
        return json.dumps({"cause": cause, "confidence": 0.93})

    mock_llm_call.side_effect = fake_classifier
    codes = generate_events(n=240, seed=42)["raw_error_code"].tolist()

    results = diagnose_batch(codes)

    assert len(results) == 240
    for r in results:
        assert r.tier in (1, 2, 3)
        assert r.status in ("resolved", "QUARANTINE")
        if r.status == "resolved":
            assert r.cause in ONTOLOGY_SET
        else:
            assert r.cause is None

    summary = tier_summary(results)
    assert summary["total"] == 240
    # all three tiers are genuinely exercised by the real dataset
    assert summary["tier1"]["count"] > 0
    assert summary["tier2"]["count"] > 0
    assert summary["tier3"]["count"] > 0
    assert summary["tier1"]["mean_confidence"] == 1.0

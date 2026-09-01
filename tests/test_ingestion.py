"""Phase 1 — Ingestion tests (from testing.md Phase 1).

Test 1: hidden ground-truth label present.
Test 2: seeded split is reproducible and leak-free.
Plus determinism / partition guards.
"""
from __future__ import annotations

import pandas as pd

from layers.ingestion import (
    ONTOLOGY_SET,
    generate_events,
    split_treatment_control,
    to_downstream_payload,
)


# --- testing.md Phase 1, Test 1 -------------------------------------------------
def test_true_cause_column_present_and_within_ontology():
    df = generate_events(n=100, seed=42)
    assert "true_cause" in df.columns
    assert df["true_cause"].isin(ONTOLOGY_SET).all()
    assert len(df) == 100


def test_downstream_payload_excludes_true_cause_keeps_raw_code():
    df = generate_events(n=100, seed=42)
    payload_df = to_downstream_payload(df)
    assert "true_cause" not in payload_df.columns
    assert "raw_error_code" in payload_df.columns  # what Detection/Diagnosis act on


# --- testing.md Phase 1, Test 2 -----------------------------------------------
def test_seeded_split_is_reproducible():
    df = generate_events(n=100, seed=42)
    treatment_df, control_df = split_treatment_control(df, seed=42)
    treatment_df_run2, control_df_run2 = split_treatment_control(df, seed=42)
    assert treatment_df.index.tolist() == treatment_df_run2.index.tolist()
    assert control_df.index.tolist() == control_df_run2.index.tolist()


def test_split_is_leak_free_and_total():
    df = generate_events(n=100, seed=42)
    treatment_df, control_df = split_treatment_control(df, seed=42)
    # every row lands in exactly one arm
    assert set(treatment_df.index).isdisjoint(control_df.index)
    assert len(treatment_df) + len(control_df) == len(df)
    # a mandate never straddles both arms
    assert set(treatment_df["mandate_id"]).isdisjoint(set(control_df["mandate_id"]))
    # downstream-facing payload still has no ground truth
    assert "true_cause" not in to_downstream_payload(treatment_df).columns


# --- determinism / sensitivity guards ---------------------------------------
def test_generate_events_is_byte_identical_for_same_seed():
    pd.testing.assert_frame_equal(
        generate_events(n=120, seed=42), generate_events(n=120, seed=42)
    )


def test_different_seed_changes_the_split():
    df = generate_events(n=100, seed=42)
    treatment_a, _ = split_treatment_control(df, seed=42)
    treatment_b, _ = split_treatment_control(df, seed=7)
    assert treatment_a.index.tolist() != treatment_b.index.tolist()


def test_novel_codes_exist_for_tier3_path():
    # `unknown` true_cause must surface raw codes absent from every known cause,
    # so Layer 3 has real Tier-3 material.
    df = generate_events(n=240, seed=42)
    known = set(df.loc[df["true_cause"] != "unknown", "raw_error_code"])
    novel = set(df.loc[df["true_cause"] == "unknown", "raw_error_code"])
    assert novel and novel.isdisjoint(known)

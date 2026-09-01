r"""Tier 2 calibration against the seeded 240-event dataset — live/demo path.

Answers one question the mocked pytest suite structurally cannot: on the real
corpus, with a real model, is `tier2_confidence_threshold` set at the right
place? It scores every distinct Tier 2 string against the dataset's hidden
`true_cause` column and reports where each landed relative to the threshold.

Like `tier2_live_check.py`, this REACHES THE NETWORK. The Tier 2 memo means
cost is one call per DISTINCT string (9), not per event (82).

    .venv\Scripts\python scripts\tier2_calibration.py

Requires GOOGLE_API_KEY in the environment or in .env.
"""
from __future__ import annotations

import sys
from collections import Counter

sys.path.insert(0, ".")

from config import settings  # noqa: E402
from layers.diagnosis import (  # noqa: E402
    _TIER2_CACHE,
    TIER1_RULES,
    diagnose,
    sanitize_input,
    tier_summary,
)
from layers.ingestion import generate_events  # noqa: E402

# `unknown` is not a wrong answer — it is the dataset's deliberate novel-string
# population, which SHOULD quarantine. Scored separately from real misses.
_UNMAPPABLE = "unknown"


def main() -> int:
    if not settings.google_api_key:
        print("GOOGLE_API_KEY is not set — set it in .env or the environment.")
        return 1

    df = generate_events(settings.dataset_n, settings.dataset_seed)
    print(f"dataset            : n={len(df)} seed={settings.dataset_seed}")
    print(f"model              : {settings.tier2_model}")
    print(f"confidence threshold: {settings.tier2_confidence_threshold}\n")

    # One row per DISTINCT raw string that Tier 1 does not already own.
    reaches_t2 = ~df["raw_error_code"].apply(
        lambda r: str(r).strip().upper() in TIER1_RULES
    )
    sub = df[reaches_t2]
    distinct = (
        sub.groupby("raw_error_code")
        .agg(volume=("raw_error_code", "size"), true_cause=("true_cause", "first"))
        .sort_values("volume", ascending=False)
    )

    # use_cache=True on purpose: this pass covers every distinct string, so it
    # populates the memo and the event-weighted pass below makes ZERO further
    # calls. Total cost is 9 requests — under the free tier's 15 RPM ceiling.
    rows = []
    for raw, meta in distinct.iterrows():
        result = diagnose(raw, use_cache=True)
        rows.append((raw, meta["volume"], meta["true_cause"], result))
        if result.status == "QUARANTINE" and "llm call failed" in result.reason:
            print(f"!! infrastructure failure, not a model verdict: {raw!r} -> {result.reason}")

    print(f"{'raw string':<36} {'vol':>4} {'true_cause':<22} "
          f"{'got':<22} {'conf':>5}  verdict")
    print("-" * 104)
    for raw, volume, truth, r in rows:
        got = r.cause if r.cause else f"QUARANTINE({r.tier})"
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "  — "
        if truth == _UNMAPPABLE:
            verdict = "OK (novel -> quarantine)" if r.status == "QUARANTINE" else "FALSE RESOLVE"
        elif r.status != "QUARANTINE":
            verdict = "correct" if r.cause == truth else "WRONG CAUSE"
        else:
            verdict = "MISS (should have resolved)"
        print(f"{raw[:36]:<36} {volume:>4} {truth:<22} {got:<22} {conf:>5}  {verdict}")

    # Event-weighted outcome: what the 240-row run actually produces. Every
    # distinct string is already memoized above, so this is pure cache reads.
    calls_before = len(_TIER2_CACHE)
    results = [diagnose(raw, use_cache=True) for raw in df["raw_error_code"]]
    if len(_TIER2_CACHE) != calls_before:
        print("!! event pass made fresh API calls — cache did not cover the corpus")
    print("\ntier distribution (all 240 events):")
    for k, v in tier_summary(results).items():
        print(f"  {k}: {v}")

    # Threshold headroom: the gap between the lowest ACCEPTED confidence and the
    # threshold is how much model drift the current setting absorbs.
    accepted = [r.confidence for r in results if r.tier == 2 and r.status == "resolved"]
    if accepted:
        lo = min(accepted)
        print(f"\nlowest accepted confidence : {lo:.2f}")
        print(f"threshold                  : {settings.tier2_confidence_threshold:.2f}")
        print(f"headroom before quarantine : {lo - settings.tier2_confidence_threshold:+.2f}")
        print(f"accepted confidences       : {dict(sorted(Counter(round(c, 2) for c in accepted).items()))}")

    misses = [r for r in rows if r[2] != _UNMAPPABLE and r[3].status == "QUARANTINE"]
    wrong = [r for r in rows if r[2] != _UNMAPPABLE and r[3].cause not in (None, r[2])]
    print(f"\nmapped strings missed by threshold : {len(misses)}")
    print(f"strings mapped to the WRONG cause  : {len(wrong)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

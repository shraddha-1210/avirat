r"""Which alt-rail gate actually decides, across the seeded 240-event dataset.

Answers the question the unit tests cannot: on real events, how often does each
gate do the deciding? Alt-rail requires BOTH a risk score over the firing
threshold AND a cost-benefit pass, so every eligible event lands in one of four
buckets — and a gate that never appears in its own bucket is dead code.

No network and no database: pure arithmetic over the seeded frame.

    .venv\Scripts\python scripts\gate_analysis.py

`days_to_next_cycle` is not a dataset column, so it is derived here the way a
monthly UPI AutoPay cycle defines it: days from the event to the 1st of the
following month. That derivation is an assumption of this report, not of the
scoring layer, which takes the number as an argument.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import pandas as pd  # noqa: E402

from config import settings  # noqa: E402
from layers.ingestion import generate_events  # noqa: E402
from layers.recovery_policy import (  # noqa: E402
    _ALT_RAIL_ELIGIBLE_CAUSES,
    ALT_RAIL_COST_RUPEES,
    map_diagnosis_to_action,
    score_recovery_risk,
)

# The shipped-before configuration, kept so the report can show the delta.
_BEFORE = {"w_amount": 0.4, "w_cost_benefit": 0.1, "cost": 12.0}


def _with_cycle_distance(df: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(df["event_ts"]).dt.tz_localize(None)
    next_cycle = (ts.dt.to_period("M") + 1).dt.to_timestamp(how="start")
    df = df.copy()
    df["days_to_next_cycle"] = (next_cycle - ts).dt.days.clip(lower=0)
    return df


def _bucket(row, *, w_amount: float, w_cost_benefit: float, cost: float) -> str:
    """Recompute the two gates under an arbitrary (weights, cost) configuration.

    Mirrors `score_recovery_risk` deliberately rather than calling it, so the
    BEFORE column can be reported without mutating global settings.
    """
    urgency = max(0.0, min(1.0, 1.0 - row.days_to_next_cycle / 30.0))
    unreliability = max(0.0, min(1.0, 1.0 - row.mandate_reliability))
    amount_tier = max(0.0, min(1.0, row.amount / 5000.0))
    expected_loss = row.amount * 0.4
    cost_benefit_passed = expected_loss > cost
    cb_norm = max(0.0, min(1.0, expected_loss / (cost * 20.0)))

    score = (
        settings.risk_weight_urgency * urgency
        + settings.risk_weight_reliability * unreliability
        + w_amount * amount_tier
        + w_cost_benefit * cb_norm
    )
    score_ok = score >= settings.risk_firing_threshold

    if score_ok and cost_benefit_passed:
        return "FIRES"
    if score_ok and not cost_benefit_passed:
        return "blocked by COST-BENEFIT"
    if not score_ok and cost_benefit_passed:
        return "blocked by SCORE"
    return "blocked by BOTH"


_BUCKETS = ("FIRES", "blocked by COST-BENEFIT", "blocked by SCORE", "blocked by BOTH")


def main() -> int:
    df = _with_cycle_distance(generate_events(settings.dataset_n, settings.dataset_seed))
    eligible = df[df["true_cause"].isin(_ALT_RAIL_ELIGIBLE_CAUSES)]

    after = {
        "w_amount": settings.risk_weight_amount,
        "w_cost_benefit": settings.risk_weight_cost_benefit,
        "cost": ALT_RAIL_COST_RUPEES,
    }

    print(f"dataset: n={len(df)} seed={settings.dataset_seed}")
    print(f"alt-rail-eligible events: {len(eligible)} "
          f"(causes: {', '.join(sorted(_ALT_RAIL_ELIGIBLE_CAUSES))})")
    print(f"firing threshold: {settings.risk_firing_threshold}\n")

    print(f"{'':<26} {'BEFORE':>10} {'AFTER':>10}")
    print(f"{'risk_weight_amount':<26} {_BEFORE['w_amount']:>10} {after['w_amount']:>10}")
    print(f"{'risk_weight_cost_benefit':<26} {_BEFORE['w_cost_benefit']:>10} "
          f"{after['w_cost_benefit']:>10}")
    print(f"{'ALT_RAIL_COST_RUPEES':<26} {_BEFORE['cost']:>10} {after['cost']:>10}")
    print()

    before_counts = eligible.apply(lambda r: _bucket(r, **_BEFORE), axis=1).value_counts()
    after_counts = eligible.apply(lambda r: _bucket(r, **after), axis=1).value_counts()

    print(f"{'gate outcome':<26} {'BEFORE':>10} {'AFTER':>10}   delta")
    print("-" * 60)
    for bucket in _BUCKETS:
        b, a = int(before_counts.get(bucket, 0)), int(after_counts.get(bucket, 0))
        print(f"{bucket:<26} {b:>10} {a:>10}   {a - b:+d}")
    print("-" * 60)

    decisive_before = int(before_counts.get("blocked by COST-BENEFIT", 0))
    decisive_after = int(after_counts.get("blocked by COST-BENEFIT", 0))
    print(f"\ncost-benefit gate decisive on: {decisive_before} events BEFORE, "
          f"{decisive_after} AFTER")
    print("=> the gate is", "REACHABLE" if decisive_after else "UNREACHABLE (dead code)")

    if decisive_after:
        print("\nevents the cost-benefit gate blocks (score passed, economics did not):")
        for row in eligible.itertuples():
            if _bucket(row, **after) != "blocked by COST-BENEFIT":
                continue
            risk = score_recovery_risk(
                days_to_next_cycle=row.days_to_next_cycle,
                mandate_reliability=row.mandate_reliability,
                amount=row.amount,
            )
            decision = map_diagnosis_to_action(
                cause=row.true_cause,
                diagnosis_tier=2,
                diagnosis_status="resolved",
                risk=risk,
            )
            print(f"  {row.event_id}  amount={row.amount}  cause={row.true_cause}")
            print(f"    score {risk.score:.3f} >= {settings.risk_firing_threshold} (passes), "
                  f"expected loss {risk.expected_loss:.2f} <= cost {risk.alt_rail_cost:.2f} "
                  f"(fails by {risk.alt_rail_cost - risk.expected_loss:.2f})")
            print(f"    -> {decision.action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""One real Tier 2 call against real ambiguous input — the live/demo path.

This is the ONLY place in the repo that intentionally reaches the network.
Every pytest run mocks `call_tier2_llm`; this script does not.

    .venv\\Scripts\\python scripts\\tier2_live_check.py

Requires GOOGLE_API_KEY in the environment or in .env.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from config import settings  # noqa: E402
from layers.diagnosis import diagnose, sanitize_input  # noqa: E402

# Genuinely ambiguous strings: verbose free text that is deliberately absent
# from the Tier 1 dict, plus one novel string that should fall to Tier 3, plus
# a hostile string that must be sanitized before it reaches the prompt.
AMBIGUOUS_INPUTS = [
    "BANK_NOT_AVAILABLE",
    "PER_TXN_LIMIT_EXCEEDED",
    "payer bank did not respond within the mandate debit window",
    "gateway declined: reason unclear",
    "<script>alert(1)</script>'; DROP TABLE mandates;--",
]


def main() -> int:
    if not settings.google_api_key:
        print("GOOGLE_API_KEY is not set — set it in .env or the environment.")
        return 1

    print(f"model: {settings.tier2_model}")
    print(f"confidence threshold: {settings.tier2_confidence_threshold}\n")

    for raw in AMBIGUOUS_INPUTS:
        result = diagnose(raw, use_cache=False)
        print(f"raw       : {raw!r}")
        print(f"sanitized : {sanitize_input(raw)!r}")
        print(
            f"-> tier {result.tier} | {result.status} | cause={result.cause} "
            f"| confidence={result.confidence}"
        )
        print(f"   {result.reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

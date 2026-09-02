"""Layer 5 cron — closes settlement holds whose window has elapsed.

DEMO SCALE: a polling loop, like the TTL watchdog. Production would schedule a
delayed message per hold, cancelled when the settlement webhook lands, so the
cost does not scale with ledger size. The arithmetic in `sweep_expired_holds()`
is identical either way; only the trigger differs, which is why the sweep is a
plain function the tests call directly with an injected clock.
"""
from __future__ import annotations

import logging
import time

from config import settings
from layers.reconciliation import sweep_expired_holds

logger = logging.getLogger(__name__)


def run_forever(*, interval_seconds: int | None = None) -> None:  # pragma: no cover - demo loop
    """Poll for expired holds. A failed sweep must not kill the loop."""
    from db import get_session

    interval = interval_seconds or settings.ttl_watchdog_interval_seconds
    logger.info(
        "settlement sweeper started (interval=%ss, hold=%ss) [demo-scale poller]",
        interval,
        settings.settlement_hold_seconds,
    )
    while True:
        try:
            with get_session() as session:
                result = sweep_expired_holds(session)
                session.commit()
            if result.escalated or result.superseded:
                logger.warning(
                    "settlement sweep: %d escalated, %d superseded",
                    result.escalated,
                    result.superseded,
                )
        except Exception:  # noqa: BLE001 — a bad sweep must not stop the sweeper
            logger.exception("settlement sweep failed; continuing to next tick")
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    run_forever()

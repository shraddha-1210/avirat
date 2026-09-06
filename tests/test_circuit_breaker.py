"""Circuit breaker + retry resilience around the Tier 2 Gemini call.

Two layers under test, deliberately kept separate:

* `CircuitBreaker` on its own, driven by an injected clock so state transitions are
  exact rather than timing-dependent. A breaker tested with `sleep()` is a breaker
  whose cooldown test is really a test of the CI machine's load average.
* The `layers.diagnosis` integration: retries, degraded-mode quarantine, and the
  guarantee that an open circuit produces a QUARANTINE rather than a guess.

`TIER2_BREAKER` is process-global, so `reset_resilience` restores it around every
test. Without that an opened circuit would leak into every suite that runs after
this file (pytest collects it before `test_diagnosis.py`) and quarantine
everything.
"""
from __future__ import annotations

import json

import pytest
from freezegun import freeze_time

import layers.diagnosis as diagnosis
from layers.circuit_breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitOpenError,
)


@pytest.fixture(autouse=True)
def reset_resilience():
    """The breaker and its counters are process-global; never let them leak."""
    diagnosis.reset_tier2_resilience()
    diagnosis.clear_tier2_cache()
    yield
    diagnosis.reset_tier2_resilience()
    diagnosis.clear_tier2_cache()


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch):
    """Retry backoff is real time; tests assert on behaviour, not on waiting."""
    slept: list[float] = []
    monkeypatch.setattr(diagnosis, "_sleep", slept.append)
    return slept


class FakeClock:
    """Injectable monotonic clock. Explicit beats freezegun for pure-unit timing."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_breaker(clock: FakeClock, **kw) -> CircuitBreaker:
    params = dict(
        failure_threshold=3,
        cooldown_seconds=60,
        half_open_test_calls=1,
        window_seconds=60,
        name="test",
        time_fn=clock,
    )
    params.update(kw)
    return CircuitBreaker(**params)


# ===========================================================================
# CircuitBreaker unit tests
# ===========================================================================
def test_starts_closed_and_allows_calls():
    cb = make_breaker(FakeClock())
    assert cb.state == CLOSED
    assert cb.allows_call() is True


def test_opens_exactly_at_the_failure_threshold():
    cb = make_breaker(FakeClock())
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CLOSED, "must not trip early"
    cb.record_failure()
    assert cb.state == OPEN


def test_open_circuit_refuses_calls():
    cb = make_breaker(FakeClock())
    for _ in range(3):
        cb.record_failure()
    assert cb.allows_call() is False


def test_failures_outside_the_window_do_not_accumulate():
    """Three failures across an hour are not an outage; three in a minute are."""
    clock = FakeClock()
    cb = make_breaker(clock)
    cb.record_failure()
    clock.advance(61)
    cb.record_failure()
    clock.advance(61)
    cb.record_failure()
    assert cb.state == CLOSED
    assert cb.snapshot().failure_count_in_window == 1


def test_success_clears_the_failure_window_while_closed():
    cb = make_breaker(FakeClock())
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == CLOSED, "the cleared window must not still be near threshold"


def test_open_transitions_to_half_open_after_cooldown():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == OPEN

    clock.advance(59)
    assert cb.state == OPEN, "must not probe early"

    clock.advance(2)
    assert cb.state == HALF_OPEN


def test_half_open_allows_exactly_n_test_calls():
    clock = FakeClock()
    cb = make_breaker(clock, half_open_test_calls=2)
    for _ in range(3):
        cb.record_failure()
    clock.advance(61)

    assert cb.allows_call() is True
    assert cb.allows_call() is True
    assert cb.allows_call() is False, "probe budget must be capped, not unlimited"


def test_half_open_success_closes_the_circuit():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    clock.advance(61)
    assert cb.allows_call() is True

    cb.record_success()

    assert cb.state == CLOSED
    assert cb.allows_call() is True


def test_half_open_failure_reopens_and_restarts_the_cooldown():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(3):
        cb.record_failure()
    clock.advance(61)
    assert cb.allows_call() is True

    cb.record_failure()

    assert cb.state == OPEN
    clock.advance(59)
    assert cb.state == OPEN, "cooldown must restart from the re-open"
    clock.advance(2)
    assert cb.state == HALF_OPEN


def test_call_helper_raises_circuit_open_without_invoking_the_callable():
    cb = make_breaker(FakeClock())
    for _ in range(3):
        cb.record_failure()

    calls = []
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: calls.append(1))
    assert calls == [], "an open circuit must not reach the dependency at all"


def test_call_helper_records_success_and_failure():
    cb = make_breaker(FakeClock())
    assert cb.call(lambda: "ok") == "ok"

    def boom():
        raise ValueError("nope")

    for _ in range(3):
        with pytest.raises(ValueError):
            cb.call(boom)
    assert cb.state == OPEN


def test_transitions_are_counted():
    clock = FakeClock()
    cb = make_breaker(clock)
    for _ in range(3):
        cb.record_failure()          # -> OPEN (1)
    clock.advance(61)
    assert cb.state == HALF_OPEN     # -> HALF_OPEN (2)
    cb.allows_call()
    cb.record_success()              # -> CLOSED (3)
    assert cb.snapshot().transitions == 3


def test_snapshot_reports_next_test_at_only_when_open():
    clock = FakeClock()
    cb = make_breaker(clock)
    assert cb.snapshot().next_test_at is None
    for _ in range(3):
        cb.record_failure()
    assert cb.snapshot().next_test_at is not None


def test_reset_returns_a_clean_closed_breaker():
    cb = make_breaker(FakeClock())
    for _ in range(3):
        cb.record_failure()
    cb.reset()
    snap = cb.snapshot()
    assert snap.state == CLOSED
    assert snap.failure_count_in_window == 0
    assert snap.transitions == 0


def test_invalid_configuration_is_refused():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0, cooldown_seconds=60)
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=3, cooldown_seconds=60, half_open_test_calls=0)


def test_breaker_is_thread_safe_under_concurrent_failures():
    """The counter must not lose writes when workers race on the same breaker."""
    import threading

    cb = make_breaker(FakeClock(), failure_threshold=1000, window_seconds=10_000)
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait(timeout=10)
        for _ in range(50):
            cb.record_failure()

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert cb.snapshot().failure_count_in_window == 20 * 50


def test_cooldown_transition_with_freezegun():
    """The same cooldown transition against a frozen wall clock, not an injected one."""
    with freeze_time("2026-01-01 00:00:00") as frozen:
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60, name="fg")
        for _ in range(3):
            cb.record_failure()
        assert cb.state == OPEN
        frozen.tick(61)
        assert cb.state == HALF_OPEN


# ===========================================================================
# diagnosis integration
# ===========================================================================
GOOD_REPLY = json.dumps({"cause": "bank_downtime", "confidence": 0.95, "rationale": "x"})


class Transient(Exception):
    """Looks like a 503 to the classifier."""

    code = 503


class ClientError(Exception):
    """Looks like a 400 to the classifier."""

    code = 400


def test_retry_recovers_after_two_transient_failures(monkeypatch, no_real_backoff):
    """Fail, fail, succeed -> the caller sees the success, not a quarantine."""
    calls = {"n": 0}

    def flaky(_sanitized, **_kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Transient("temporarily unavailable")
        return GOOD_REPLY

    monkeypatch.setattr(diagnosis, "call_tier2_llm", flaky)

    result = diagnosis.diagnose_tier2("BANK_NOT_AVAILABLE", use_cache=False)

    assert calls["n"] == 3
    assert result.tier == 2
    assert result.status == "resolved"
    assert result.cause == "bank_downtime"
    assert diagnosis.TIER2_BREAKER.state == CLOSED
    metrics = diagnosis.tier2_metrics()
    assert metrics["retries_attempted"] == 2
    assert metrics["retries_succeeded"] == 1
    assert len(no_real_backoff) == 2, "one jittered sleep per retry"


def test_client_error_is_not_retried(monkeypatch, no_real_backoff):
    """A 4xx will be rejected again identically; retrying only wastes the budget."""
    calls = {"n": 0}

    def bad_request(_sanitized, **_kw):
        calls["n"] += 1
        raise ClientError("invalid argument")

    monkeypatch.setattr(diagnosis, "call_tier2_llm", bad_request)

    result = diagnosis.diagnose_tier2("BANK_NOT_AVAILABLE", use_cache=False)

    assert calls["n"] == 1, "4xx must not be retried"
    assert no_real_backoff == []
    assert result.tier == 3
    assert result.status == "QUARANTINE"
    assert diagnosis.tier2_metrics()["retries_attempted"] == 0


def test_malformed_json_is_not_retried_and_does_not_trip_the_breaker(monkeypatch):
    """Gemini answered; the reply was unusable. That is not the dependency being down."""
    calls = {"n": 0}

    def garbage(_sanitized, **_kw):
        calls["n"] += 1
        return "not json at all"

    monkeypatch.setattr(diagnosis, "call_tier2_llm", garbage)

    for _ in range(5):
        result = diagnosis.diagnose_tier2("BANK_NOT_AVAILABLE", use_cache=False)
        assert result.status == "QUARANTINE"

    assert calls["n"] == 5, "no retries on a parse failure"
    assert diagnosis.TIER2_BREAKER.state == CLOSED, "a bad reply is not an outage"


def test_schema_violation_does_not_trip_the_breaker(monkeypatch):
    monkeypatch.setattr(
        diagnosis, "call_tier2_llm",
        lambda _s, **_k: json.dumps({"cause": "bank_downtime", "confidence": 2.5}),
    )
    for _ in range(5):
        assert diagnosis.diagnose_tier2("X", use_cache=False).status == "QUARANTINE"
    assert diagnosis.TIER2_BREAKER.state == CLOSED


def test_three_consecutive_outages_open_the_circuit(monkeypatch):
    """The integration path: 3 logical call failures -> OPEN, then calls are skipped."""
    calls = {"n": 0}

    def down(_sanitized, **_kw):
        calls["n"] += 1
        raise ClientError("permission denied")

    monkeypatch.setattr(diagnosis, "call_tier2_llm", down)

    for _ in range(3):
        diagnosis.diagnose_tier2("BANK_NOT_AVAILABLE", use_cache=False)

    assert diagnosis.TIER2_BREAKER.state == OPEN
    assert calls["n"] == 3

    # Next call must be skipped entirely, not attempted.
    result = diagnosis.diagnose_tier2("SOMETHING_ELSE", use_cache=False)
    assert calls["n"] == 3, "an open circuit must not reach Gemini"
    assert result.status == "QUARANTINE"
    assert "degraded mode" in result.reason


def test_open_circuit_quarantines_rather_than_guessing(monkeypatch):
    """Fail closed. A degraded dependency must never become a confident answer."""
    monkeypatch.setattr(
        diagnosis, "call_tier2_llm",
        lambda _s, **_k: (_ for _ in ()).throw(ClientError("bad key")),
    )
    for _ in range(3):
        diagnosis.diagnose_tier2("A", use_cache=False)
    assert diagnosis.TIER2_BREAKER.state == OPEN

    result = diagnosis.diagnose_tier2("BANK_NOT_AVAILABLE", use_cache=False)

    assert result.tier == 3
    assert result.status == "QUARANTINE"
    assert result.cause is None, "degraded mode must not produce a classification"
    assert result.reason == "gemini circuit open (degraded mode)"
    assert result.llm_model is None, (
        "no call was made, so naming a model would undo the distinction the "
        "breaker exists to draw"
    )


def test_full_recovery_cycle_open_then_half_open_then_closed(monkeypatch):
    """The whole loop: open on failure, probe after cooldown, close on success."""
    state = {"fail": True, "calls": 0}

    def toggling(_sanitized, **_kw):
        state["calls"] += 1
        if state["fail"]:
            raise Transient("unavailable")
        return GOOD_REPLY

    monkeypatch.setattr(diagnosis, "call_tier2_llm", toggling)

    # Each diagnose is one logical call: 1 attempt + 2 retries = 3 SDK calls.
    for _ in range(3):
        diagnosis.diagnose_tier2("A", use_cache=False)
    assert diagnosis.TIER2_BREAKER.state == OPEN

    skipped_at = state["calls"]
    assert diagnosis.diagnose_tier2("B", use_cache=False).reason == (
        "gemini circuit open (degraded mode)"
    )
    assert state["calls"] == skipped_at, "OPEN must skip"

    # Advance past the cooldown, and let the dependency recover.
    diagnosis.TIER2_BREAKER._opened_at -= settings_cooldown() + 1
    assert diagnosis.TIER2_BREAKER.state == HALF_OPEN

    state["fail"] = False
    result = diagnosis.diagnose_tier2("BANK_NOT_AVAILABLE", use_cache=False)

    assert result.tier == 2 and result.status == "resolved"
    assert diagnosis.TIER2_BREAKER.state == CLOSED


def settings_cooldown() -> float:
    from config import settings

    return float(settings.circuit_cooldown_seconds)


def test_half_open_probe_failure_reopens_in_the_pipeline(monkeypatch):
    monkeypatch.setattr(
        diagnosis, "call_tier2_llm",
        lambda _s, **_k: (_ for _ in ()).throw(ClientError("still down")),
    )
    for _ in range(3):
        diagnosis.diagnose_tier2("A", use_cache=False)
    assert diagnosis.TIER2_BREAKER.state == OPEN

    diagnosis.TIER2_BREAKER._opened_at -= settings_cooldown() + 1
    assert diagnosis.TIER2_BREAKER.state == HALF_OPEN

    diagnosis.diagnose_tier2("B", use_cache=False)
    assert diagnosis.TIER2_BREAKER.state == OPEN, "a failed probe must re-open"


def test_cache_hit_neither_consults_nor_informs_the_breaker(monkeypatch):
    """A memo hit reaches no dependency; letting it close the circuit would be a lie."""
    monkeypatch.setattr(diagnosis, "call_tier2_llm", lambda _s, **_k: GOOD_REPLY)
    diagnosis.diagnose_tier2("BANK_NOT_AVAILABLE", use_cache=True)

    monkeypatch.setattr(
        diagnosis, "call_tier2_llm",
        lambda _s, **_k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    for _ in range(3):
        diagnosis.TIER2_BREAKER.record_failure()
    assert diagnosis.TIER2_BREAKER.state == OPEN

    result = diagnosis.diagnose_tier2("BANK_NOT_AVAILABLE", use_cache=True)
    assert result.tier == 2 and result.status == "resolved"


def test_tier1_is_unaffected_by_an_open_circuit(monkeypatch):
    """Tier 1 never touches Gemini, so a dead dependency must not degrade it."""
    for _ in range(3):
        diagnosis.TIER2_BREAKER.record_failure()
    assert diagnosis.TIER2_BREAKER.state == OPEN

    result = diagnosis.diagnose("U14", use_cache=False)
    assert result.tier == 1
    assert result.cause == "insufficient_funds"
    assert result.confidence == 1.0


# ===========================================================================
# health endpoint
# ===========================================================================
def test_health_endpoint_reports_closed_state():
    from fastapi.testclient import TestClient

    import app as app_module

    body = TestClient(app_module.app).get("/api/health/gemini").json()
    assert body["state"] == "CLOSED"
    assert body["failure_count_last_60s"] == 0
    assert body["last_failure_at"] is None
    assert body["next_test_at"] is None


def test_health_endpoint_reports_open_state_with_next_test_at():
    from fastapi.testclient import TestClient

    import app as app_module

    for _ in range(3):
        diagnosis.TIER2_BREAKER.record_failure()

    body = TestClient(app_module.app).get("/api/health/gemini").json()
    assert body["state"] == "OPEN"
    assert body["failure_count_last_60s"] == 3
    assert body["last_failure_at"] is not None
    assert body["next_test_at"] is not None
    assert body["circuit_transitions"] >= 1

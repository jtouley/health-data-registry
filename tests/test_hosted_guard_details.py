"""The hosted guard's details the round trips leave loose: thread kinds, exact statuses, budgets and log lines.

Each test here pins a rule the nightly mutation run (hard rule 13; issue #212) showed no other test would notice
breaking: a worker thread that would keep the process alive, a busy call counted as an error, a deadline measured
from the wrong end, a waiting counter that drifts, a restart that never comes.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from health_data_registry import deadline, hosted
from health_data_registry.deadline import DeadlineExceeded
from health_data_registry.hosted import CallGuard, HostedConfig, Metrics


def cfg(tmp_path: Path, **kw: Any) -> HostedConfig:
    base: dict[str, Any] = {"hub_url": "unused", "cache": tmp_path / "cache", "tool_timeout_s": 5.0,
                            "max_result_chars": 140_000, "rate_per_session": 30, "rate_global": 300,
                            "metrics_every_s": 3600}  # fmt: skip
    return HostedConfig(**{**base, **kw})


def statuses(metrics: Metrics) -> list[str]:
    return [status for (_tool, status, _bucket) in metrics.counts]


# ------------------------------------------------------------------------------------------ deadline.bounded


def test_bounded_runs_on_a_named_daemon_helper_inside_a_deadline() -> None:
    """A helper that outlives its call must never keep the process from exiting."""
    seen: list[tuple[bool, str]] = []

    def probe() -> str:
        me = threading.current_thread()
        seen.append((me.daemon, me.name))
        return "done"

    with deadline.call_deadline(5):
        assert deadline.bounded(probe) == "done"
    assert seen == [(True, "deadline-bounded")]


def test_bounded_with_exactly_nothing_left_never_starts_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(deadline, "remaining", lambda: 0.0)

    def run() -> str:
        ran.append("ran")
        return "ran"

    with pytest.raises(DeadlineExceeded):
        deadline.bounded(run)
    assert ran == []


# ------------------------------------------------------------------------------------------ the guard's call


def test_the_worker_is_a_daemon_thread_named_for_its_tool(tmp_path: Path) -> None:
    seen: list[tuple[bool, str]] = []

    def probe() -> str:
        me = threading.current_thread()
        seen.append((me.daemon, me.name))
        return "ok"

    assert CallGuard(cfg(tmp_path), Metrics())("get_rate", probe, "csv") == "ok"
    assert seen == [(True, "tool-get_rate")]


def test_a_failure_without_a_code_is_counted_as_an_error(tmp_path: Path) -> None:
    metrics = Metrics()

    def boom() -> str:
        raise KeyError("boom")

    out = CallGuard(cfg(tmp_path), metrics)("q", boom, "json")
    assert isinstance(out, dict) and out["refused"] == "error"
    assert statuses(metrics) == ["error"]


def test_a_deadline_inside_the_body_answers_with_the_whole_budget(tmp_path: Path) -> None:
    def late() -> str:
        raise DeadlineExceeded()

    metrics = Metrics()
    out = CallGuard(cfg(tmp_path, tool_timeout_s=5.0), metrics)("q", late, "json")
    assert out == hosted.tools.refusal(DeadlineExceeded(5.0), "json")
    assert isinstance(out, dict) and "(5 seconds)" in out["message"]
    assert statuses(metrics) == ["timeout"]


def test_a_stuck_worker_is_answered_in_the_format_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "GRACE_S", 0.05)
    release = threading.Event()
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.1), Metrics())
    try:
        out = guard("q", lambda: release.wait(5) and "late", "json")
        assert out == hosted.tools.refusal(DeadlineExceeded(0.1), "json")
        assert isinstance(out, dict) and out["refused"] == "too_slow"
    finally:
        release.set()


def test_a_stuck_worker_that_ends_never_releases_a_lock_it_gave_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's own release must see the guard already took the lock back, and do nothing."""
    monkeypatch.setattr(hosted, "GRACE_S", 0.05)
    errors: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    release = threading.Event()
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.1), Metrics())
    guard("q", lambda: release.wait(5) and "late", "csv")
    release.set()
    for _ in range(200):
        if not guard.poisoned():
            break
        time.sleep(0.01)
    assert not guard.poisoned()
    assert errors == []  # no "release unlocked lock" from the worker
    assert not guard._lock.locked()


def test_busy_and_poisoned_calls_are_counted_as_such(tmp_path: Path) -> None:
    metrics = Metrics()
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.2), metrics)
    guard._lock.acquire()
    try:
        assert guard("q", lambda: "x", "csv") == hosted.tools.message(hosted.BUSY, "csv", refused="busy")
    finally:
        guard._lock.release()
    assert statuses(metrics) == ["busy"]

    metrics2 = Metrics()
    guard2 = CallGuard(cfg(tmp_path, tool_timeout_s=2), metrics2)
    guard2._lock.acquire()
    stuck = threading.Thread(target=time.sleep, args=(0.4,))
    result: list[Any] = []
    waiter = threading.Thread(target=lambda: result.append(guard2("q", lambda: "ran", "json")))
    waiter.start()
    time.sleep(0.1)  # the call now waits for the engine
    stuck.start()
    guard2._stuck.append(stuck)
    guard2._lock.release()
    waiter.join(3)
    stuck.join()
    assert result == [{"message": hosted.POISONED, "refused": "busy", "next_step": "Try again in a minute."}]
    assert statuses(metrics2) == ["poisoned"]


def test_time_spent_before_the_engine_counts_against_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait for the engine and the body's deadline are what is left of the budget, never more."""
    monkeypatch.setattr(hosted, "GRACE_S", 0.15)
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.6), Metrics())
    real = guard.poisoned
    calls = [0]

    def slow_first_check() -> bool:
        calls[0] += 1
        if calls[0] == 1:
            time.sleep(0.35)
        return real()

    monkeypatch.setattr(guard, "poisoned", slow_first_check)
    left: list[float | None] = []

    def body() -> str:
        left.append(deadline.remaining())
        return "ok"

    assert guard("q", body, "csv") == "ok"
    assert left and left[0] is not None and left[0] < 0.3

    calls[0] = 0
    guard._lock.acquire()
    try:
        out = guard("q", lambda: "x", "json")
    finally:
        guard._lock.release()
    assert isinstance(out, dict) and out["message"] == hosted.BUSY  # gave up waiting at the budget, not after


def test_waiting_for_the_engine_is_bounded_by_the_budget_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "GRACE_S", 5.0)
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=0.2), Metrics())
    guard._lock.acquire()
    try:
        start = time.monotonic()
        out = guard("q", lambda: "x", "csv")
        took = time.monotonic() - start
    finally:
        guard._lock.release()
    assert isinstance(out, str) and out.startswith(hosted.BUSY)
    assert 0.2 <= took < 0.8


def test_the_waiting_count_returns_to_zero_after_calls_that_queued(tmp_path: Path) -> None:
    guard = CallGuard(cfg(tmp_path, tool_timeout_s=3), Metrics())
    assert guard("q", lambda: "x", "csv") == "x" and guard._waiting == 0
    guard._lock.acquire()
    out: list[Any] = []
    workers = [
        threading.Thread(target=lambda: out.append(guard("q", lambda: "ran", "csv"))) for _ in range(2)
    ]
    for w in workers:
        w.start()
    for _ in range(200):
        with guard._state:
            if guard._waiting == 2:
                break
        time.sleep(0.005)
    with guard._state:
        assert guard._waiting == 2
    guard._lock.release()
    for w in workers:
        w.join(5)
    assert out == ["ran", "ran"] and guard._waiting == 0


# ------------------------------------------------------------------------------------------ poisoning


class _Iso:
    bad = True

    def poisoned(self) -> bool:
        return self.bad


def test_without_an_exit_function_the_guard_exits_with_ex_software(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes: list[int] = []
    monkeypatch.setattr(hosted.os, "_exit", codes.append)
    CallGuard(cfg(tmp_path), Metrics())._exit()
    assert codes == [70]


def test_poisoning_is_logged_with_its_reasons_and_restarts_at_exactly_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Eng:
        isolation = _Iso()

    Eng.isolation.bad = True
    monkeypatch.setattr(hosted, "stragglers", lambda: 1)
    now = [100.0]
    exits: list[str] = []
    guard = CallGuard(cfg(tmp_path, poisoned_exit_s=60), Metrics(), Eng(), clock=lambda: now[0],
                      exit_fn=lambda: exits.append("exit"))  # fmt: skip
    with caplog.at_level("WARNING", logger=hosted.METRICS_LOGGER):
        assert guard.poisoned()
        assert guard.poison_reasons() == [
            "a hub download is still running after its call gave up",
            "a query process did not stop when it was killed",
        ]
        now[0] = 130.27
        status = guard.poison_status()
        assert status["poisoned_for_s"] == 30.3
        assert str(status["poisoned_since"]).endswith("+00:00")
        now[0] = 160.0  # exactly poisoned_exit_s after the onset
        assert guard.poisoned() and exits == ["exit"]
        monkeypatch.setattr(hosted, "stragglers", lambda: 0)
        Eng.isolation.bad = False
        now[0] = 170.0
        assert not guard.poisoned()
    messages = [r.getMessage() for r in caplog.records if r.name == hosted.METRICS_LOGGER]
    assert messages == [
        "engine poisoned: a hub download is still running after its call gave up; a query process did not stop"
        " when it was killed; questions are refused until it ends",
        "engine poisoned for over 60 s; exiting so the platform restarts the server",
        "engine no longer poisoned after 70 s",
    ]


# ------------------------------------------------------------------------------------------ warm start


class _Hub:
    def __init__(self) -> None:
        self.released = 0

    def revision(self) -> str:
        return "rev-1"

    def release(self) -> None:
        self.released += 1


def _state(tmp_path: Path, engine: Any) -> hosted.Hosted:
    return hosted.Hosted(
        cfg(tmp_path), None, engine, CallGuard(cfg(tmp_path), Metrics()), cast(Any, None), Metrics()
    )


def test_the_warm_start_resolves_the_release_and_reads_its_manifest(tmp_path: Path) -> None:
    connected: list[bool] = []

    class Eng:
        hub = _Hub()

        def connect(self) -> None:
            connected.append(True)

    state = _state(tmp_path, Eng())
    state.warm_up()
    assert state.warm == {"done": True, "release": "rev-1"}
    assert Eng.hub.released == 1 and connected == [True]


def test_the_warm_start_needs_no_isolation_and_survives_a_failed_measure(tmp_path: Path) -> None:
    class Plain:
        hub = None

        def connect(self) -> None:
            return None

    state = _state(tmp_path, Plain())
    state.warm_up()  # an engine without isolation: nothing to measure
    assert state.warm["done"] is True

    class Iso:
        def measure(self) -> bool:
            raise OSError("no child")

    class Measured(Plain):
        isolation = Iso()

    state = _state(tmp_path, Measured())
    state.warm_up()
    assert state.warm["done"] is True


def test_a_failed_warm_start_logs_only_the_failure_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Coded(Exception):
        code = "hub_unreachable"

    class Eng:
        hub = None
        error: Exception = Coded("secret detail")

        def connect(self) -> None:
            raise self.error

    with caplog.at_level(logging.INFO, logger=hosted.METRICS_LOGGER):
        _state(tmp_path, Eng()).warm_up()
        Eng.error = OSError("secret detail")
        _state(tmp_path, Eng()).warm_up()
    messages = [r.getMessage() for r in caplog.records if r.name == hosted.METRICS_LOGGER]
    assert messages == ["warm start incomplete: hub_unreachable", "warm start incomplete: OSError"]


# ------------------------------------------------------------------------------------------ start-up limits


def test_a_page_above_the_result_cap_refuses_to_start_in_plain_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HDR_PAGE_CHARS", "200000")
    with pytest.raises(ValueError) as e:
        hosted.check_limits(cfg(tmp_path, max_result_chars=140_000))
    assert str(e.value) == (
        "HDR_PAGE_CHARS (200,000) is above HDR_MAX_RESULT_CHARS (140,000), so long answers would be refused "
        "instead of paged. Set HDR_PAGE_CHARS below the cap, or unset both."
    )

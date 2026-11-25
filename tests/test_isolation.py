"""Issue #64 and #74 item 1: every hosted query runs in a child process with a memory limit and a hard kill."""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import math
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import isolation
from health_data_registry.deadline import DeadlineExceeded, call_deadline
from health_data_registry.isolation import (
    POISONED,
    TOO_MUCH_MEMORY,
    TOO_SLOW,
    Isolation,
    IsolationRefused,
    Job,
    Source,
    decode,
    encode,
    execute,
)
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry
from health_data_registry.sql_guard import guard_sql

LINUX = sys.platform.startswith("linux")
# One function call DuckDB's interrupt can't stop, and whose 1.5 GB string its memory_limit doesn't count.
HUGE = "SELECT length(repeat('x', 1500000000)) AS n"


def snapshot_db(lake: Path) -> Path:
    found = sorted(lake.glob("datasets/toy_cases/run_date=*/*/data.duckdb"))
    assert found
    return found[-1]


def sources(lake: Path) -> tuple[Source, ...]:
    return (Source("attach", "toy_cases", path=str(snapshot_db(lake))),)


class Recording(Isolation):
    """Remembers every child it started, to check each one is gone when run() returns."""

    procs: list[Any]

    def _reap(self, proc: Any) -> None:
        self.__dict__.setdefault("procs", []).append(proc)
        super()._reap(proc)


# --------------------------------------------------------------------------------------- the reply codec


def test_replies_round_trip_every_engine_type_without_pickle() -> None:
    values = [
        None, True, 3, 2.5, "text", dt.date(2024, 1, 2), dt.datetime(2024, 1, 2, 3, 4, 5),
        dt.datetime(2024, 1, 2, 3, 4, tzinfo=dt.UTC), dt.time(1, 2, 3), dt.timedelta(days=2, seconds=5, microseconds=7),
        decimal.Decimal("1.10"), uuid.UUID(int=5), b"\x00\xff", [1, [dt.date(2020, 1, 1)]],
        {"a": 1, "b": [2]}, {(1, 2): "pair"},
    ]  # fmt: skip
    status, cols, rows = decode(encode(("ok", ["c"], [values])))
    assert status == "ok" and cols == ["c"]
    assert rows == [[*values[:-1], {(1, 2): "pair"}]]
    assert type(rows[0][10]) is decimal.Decimal and type(rows[0][5]) is dt.date
    nan = decode(encode(("ok", [], [[math.nan]])))[2][0][0]
    assert math.isnan(nan)
    assert decode(encode(("ok", [], [[object]])))[2][0][0] == str(object)  # no JSON form: its text
    assert decode(b'["ok",[],[{"plain":1}]]')[2] == [{"plain": 1}]  # an untagged object stays as it is
    with pytest.raises(ValueError, match="unknown tag"):
        decode(b'["ok",[],[{"$t":"pickle","v":"x"}]]')


# ------------------------------------------------------------------------------- the child's own work


def test_execute_runs_trusted_and_guarded_sql_on_a_read_only_copy(lake: Path, registry: Registry) -> None:
    src = sources(lake)
    sql = "SELECT reporting_area, cases FROM toy_cases WHERE year = 2023 ORDER BY ALL"
    status, cols, rows = execute(Job(src, sql=sql))
    assert status == "ok" and cols == ["reporting_area", "cases"] and len(rows) == 4
    assert execute(Job(src, sql=sql, limit=1))[2] == [rows[0]]
    assert execute(Job(src, sql="SELECT ? + 1 AS x", params=(41,)))[2] == [[42]]
    q = guard_sql(
        sql, allowed_tables={"toy_cases": registry.get("toy_cases").contract.column_names}, max_rows=10
    )
    assert execute(Job(src, guarded=q))[1:] == (cols, rows)
    # The copy is attached READ_ONLY and the connection is hardened: no write, no file outside the catalogue.
    for bad in ('CREATE TABLE "snap_toy_cases".x AS SELECT 1', "SELECT * FROM read_csv('/etc/passwd')"):
        status, code, message = execute(Job(src, sql=bad))
        assert status == "refused" and code == "query_failed" and "could not run" in message


def test_execute_answers_every_failure_in_plain_words(lake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import duckdb

    q = guard_sql(
        "SELECT cases FROM toy_cases", allowed_tables={"toy_cases": frozenset({"cases"})}, max_rows=5
    )
    unhardened = Job(sources(lake), guarded=q)
    monkeypatch.setattr(isolation, "build_connection", lambda *a: duckdb.connect(":memory:"))
    assert execute(unhardened)[:2] == ("refused", "not_hardened")
    for exc, code, words in (
        (MemoryError(), "too_much_memory", TOO_MUCH_MEMORY),
        (duckdb.OutOfMemoryException("x"), "too_much_memory", TOO_MUCH_MEMORY),
        (duckdb.InterruptException("x"), "too_slow", TOO_SLOW),
        (KeyError("secret path"), "query_failed", f"{isolation.FAILED} (KeyError)"),
    ):

        def boom(*_: Any, e: BaseException = exc) -> Any:
            raise e

        monkeypatch.setattr(isolation, "build_connection", boom)
        assert execute(Job(()))[1:] == (code, words)


def test_unknown_sources_are_refused(tmp_path: Path) -> None:
    for s in (Source("ftp", "x", path="/x"), Source("attach", "x"), Source("live", "x")):
        with pytest.raises(ValueError, match="unknown source kind"):
            isolation.build_connection([s])


def test_the_child_sets_its_limits_where_the_platform_has_them(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[int, int]]] = []

    class Resource:
        RLIMIT_AS, RLIMIT_FSIZE, RLIMIT_CORE = "as", "fsize", "core"

        @staticmethod
        def setrlimit(which: str, value: tuple[int, int]) -> None:
            calls.append((which, value))

    monkeypatch.setitem(sys.modules, "resource", Resource)
    assert isolation.limit_child(1234) is LINUX
    assert calls == [("fsize", (0, 0)), ("core", (0, 0)), ("as", (1234, 1234))]
    calls.clear()
    assert isolation.limit_child(None) is False and [c[0] for c in calls] == ["fsize", "core"]

    class Refuses(Resource):
        @staticmethod
        def setrlimit(which: str, value: tuple[int, int]) -> None:
            raise ValueError("not on this kernel")  # macOS refuses RLIMIT_AS

    monkeypatch.setitem(sys.modules, "resource", Refuses)
    assert isolation.limit_child(1234) is False
    monkeypatch.setitem(sys.modules, "resource", None)  # Windows: no resource module
    assert isolation.limit_child(1234) is False


def test_limits_come_from_the_environment_and_bad_ones_refuse_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iso = Isolation()
    assert (iso.memory_mb, iso.result_mb) == (isolation.DEFAULT_MEMORY_MB, isolation.DEFAULT_RESULT_MB)
    monkeypatch.setenv("HDR_QUERY_MEMORY_MB", "2048")
    monkeypatch.setenv("HDR_QUERY_RESULT_MB", "1")
    assert (Isolation().memory_mb, Isolation().result_mb) == (2048, 1)
    for bad in ("1023", "lots", "-5", "0"):
        monkeypatch.setenv("HDR_QUERY_MEMORY_MB", bad)
        with pytest.raises(
            ValueError, match="HDR_QUERY_MEMORY_MB must be a whole number of MiB, at least 1,024"
        ):
            Isolation()
    monkeypatch.delenv("HDR_QUERY_MEMORY_MB")
    monkeypatch.setenv("HDR_QUERY_RESULT_MB", "0")
    with pytest.raises(ValueError, match="HDR_QUERY_RESULT_MB"):
        Isolation()
    assert isolation.start_method() == ("forkserver" if LINUX or sys.platform == "darwin" else "spawn")
    status = iso.status()
    assert status == {"poisoned": False, "memory_limit_mb": 4096, "memory_limit_enforced": LINUX,
                      "start_method": isolation.start_method()}  # fmt: skip


# ------------------------------------------------------------------------------- the child process


def test_a_query_runs_in_a_child_and_the_child_is_gone_after(lake: Path) -> None:
    iso = Recording()
    cols, rows = iso.run(Job(sources(lake), sql="SELECT count(*) AS n FROM toy_cases"))
    assert (cols, rows) == (["n"], [[6]])
    assert len(iso.procs) == 1 and not iso.procs[0].is_alive() and iso.procs[0].exitcode == 0


def test_an_uninterruptible_call_is_killed_at_the_deadline(lake: Path) -> None:
    """A single long function call ignores con.interrupt(); the child is killed at the deadline anyway."""
    iso = Recording(memory_mb=16_384)  # memory is not what stops it here
    start = time.monotonic()
    with pytest.raises(IsolationRefused) as e:
        iso.run(Job(sources(lake), sql=HUGE, timeout_s=0.3))
    took = time.monotonic() - start
    assert e.value.code == "too_slow" and e.value.message == TOO_SLOW
    assert took < 0.3 + isolation.KILL_MARGIN_S + 1.0  # the query alone runs for several seconds
    assert not iso.procs[0].is_alive() and iso.procs[0].exitcode == -9  # SIGKILLed, and reaped


def test_the_call_deadline_kills_the_child_and_says_so(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake, isolation=Recording(memory_mb=16_384))
    eng.connect()
    start = time.monotonic()
    with call_deadline(0.4), pytest.raises(DeadlineExceeded):
        eng._run(HUGE)
    assert time.monotonic() - start < 2.0
    with pytest.raises(QueryRefused) as e:  # outside a call deadline: the job's own limit, in plain words
        eng._run(HUGE, timeout_s=0.2)
    assert (e.value.code, str(e.value)) == ("too_slow", TOO_SLOW)


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS is enforced only on Linux (the Space)")
def test_memory_over_the_limit_is_refused_not_an_out_of_memory_of_the_server(lake: Path) -> None:
    iso = Isolation(memory_mb=1536)  # DuckDB's baseline (~1 GiB of address space) plus a little
    with pytest.raises(IsolationRefused) as e:
        iso.run(Job(sources(lake), sql=HUGE, timeout_s=30))
    assert e.value.code == "too_much_memory" and e.value.message == TOO_MUCH_MEMORY
    assert iso.run(Job(sources(lake), sql="SELECT 1 AS x")) == (["x"], [[1]])  # the server carries on


def test_a_reply_over_the_cap_is_never_read(lake: Path) -> None:
    iso = Isolation(result_mb=1)
    with pytest.raises(IsolationRefused) as e:
        iso.run(Job(sources(lake), sql="SELECT repeat('x', 2000000) AS big"))
    assert e.value.code == "too_large" and "over 1 MiB" in e.value.message

    class Over:
        def poll(self, _: float) -> bool:
            return True

        def recv_bytes(self, _: int) -> bytes:
            raise OSError("bad message length")  # what Connection.recv_bytes(maxlength) raises

    class Proc:
        killed = False

        def kill(self) -> None:
            Proc.killed = True

    with pytest.raises(IsolationRefused) as e2:
        iso._read(Over(), Proc(), 1.0, 10)
    assert e2.value.code == "too_large" and Proc.killed


def test_a_timed_out_worker_cant_change_shared_state_after_the_next_request(
    lake: Path, registry: Registry
) -> None:
    """The killed child is reaped before run() returns, held only read-only files, and its reply is never read."""
    db = snapshot_db(lake)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    iso = Recording(memory_mb=16_384)
    eng = Engine(registry=registry, lake=lake, isolation=iso)
    with call_deadline(0.3), pytest.raises(DeadlineExceeded):
        eng._run(HUGE)
    stale = iso.procs[0]
    assert not stale.is_alive()  # gone before the next request starts
    out = eng.query_metric("toy_cases", "case_count", filters={"condition": "Legionellosis", "year": 2023})
    assert out["rows"] == [[710, 0]]
    assert not stale.is_alive() and hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert iso.poisoned() is False


def test_isolated_answers_match_in_process_answers(lake: Path, registry: Registry) -> None:
    inproc = Engine(registry=registry, lake=lake)
    iso = Engine(registry=registry, lake=lake, isolation=Isolation())
    args: dict[str, Any] = {"filters": {"condition": "Legionellosis"}, "group_by": ["state", "year"]}
    assert iso.query_metric("toy_cases", "case_count", **args) == inproc.query_metric(
        "toy_cases", "case_count", **args
    )
    sql = "SELECT reporting_area, year, cases FROM toy_cases"
    assert iso.query_sql(sql) == inproc.query_sql(sql)
    assert iso.distinct_values("toy_cases", "reporting_area") == inproc.distinct_values(
        "toy_cases", "reporting_area"
    )
    assert (
        iso.loaded_values("toy_cases", "year")
        == inproc.loaded_values("toy_cases", "year")
        == [2022, 2023, 2024]
    )
    with pytest.raises(QueryRefused) as e:  # a failure inside the child comes back as the guard's own words
        iso.query_sql("SELECT CAST(reporting_area AS INTEGER) AS n FROM toy_cases")
    assert (e.value.code, str(e.value)) == (
        "query_failed",
        "A value could not be converted to the type the query needs.",
    )
    pinned = iso.at({})
    assert pinned is iso and pinned.isolation is iso.isolation


def test_loaded_values_give_up_quietly_when_the_child_refuses(lake: Path, registry: Registry) -> None:
    class Refusing(Isolation):
        def run(self, job: Job, timeout_s: float | None = None) -> tuple[list[str], list[list[Any]]]:
            assert job.timeout_s == 5.0  # describe_dataset's own limit
            raise IsolationRefused("too_slow", TOO_SLOW)

    eng = Engine(registry=registry, lake=lake, isolation=Refusing())
    assert eng.loaded_values("toy_cases", "year") is None


# ------------------------------------------------------------------------------------- poisoned


class Unkillable:
    """A child stuck in the kernel: SIGKILL doesn't take, join never returns it."""

    alive = True
    exitcode = None

    def join(self, _: float | None = None) -> None:
        return None

    def is_alive(self) -> bool:
        return self.alive

    def kill(self) -> None:
        return None


def test_a_child_that_cant_be_reaped_poisons_the_engine_until_it_exits(lake: Path) -> None:
    iso = Isolation(kill_wait_s=0.01)
    stuck = Unkillable()
    iso._reap(stuck)
    assert iso.poisoned() and iso.status()["poisoned"] is True
    with pytest.raises(IsolationRefused) as e:
        iso.run(Job(sources(lake), sql="SELECT 1 AS x"))
    assert (e.value.code, e.value.message) == ("busy", POISONED)
    stuck.alive = False  # it finally exits
    assert not iso.poisoned() and iso.run(Job(sources(lake), sql="SELECT 1 AS x")) == (["x"], [[1]])


def test_why_a_child_gave_no_reply() -> None:
    class P:
        def __init__(self, code: int | None) -> None:
            self.exitcode = code

    assert Isolation._why(P(-9), killed=True) == ("too_slow", TOO_SLOW)
    assert Isolation._why(P(-11), killed=False) == ("too_much_memory", TOO_MUCH_MEMORY)
    assert Isolation._why(P(1), killed=False) == ("query_failed", isolation.STOPPED)
    assert Isolation._why(P(None), killed=False) == ("query_failed", isolation.STOPPED)
    with pytest.raises(IsolationRefused) as e:
        Isolation().run(Job(()), timeout_s=0)
    assert e.value.code == "too_slow"


def test_overhead_of_a_child_per_query_is_small(lake: Path, registry: Registry) -> None:
    """Not a benchmark: a guard against a start method that loads Python and DuckDB for every query."""
    inproc = Engine(registry=registry, lake=lake)
    iso = Engine(registry=registry, lake=lake, isolation=Isolation())
    args: dict[str, Any] = {"filters": {"condition": "Legionellosis", "year": 2023}}
    iso.query_metric("toy_cases", "case_count", **args)  # starts the forkserver once
    start = time.monotonic()
    for _ in range(5):
        iso.query_metric("toy_cases", "case_count", **args)
    per_query = (time.monotonic() - start) / 5
    start = time.monotonic()
    for _ in range(5):
        inproc.query_metric("toy_cases", "case_count", **args)
    assert per_query - (time.monotonic() - start) / 5 < 1.0


def test_a_restricted_live_table_is_rebuilt_in_the_child_from_memory(tmp_path: Path) -> None:
    """A live source (never stored) reaches the child as records over the pipe, never as a file."""
    from health_data_registry.registry import load_registry
    from tests.test_michigan_models import COVID_ROWS, ROOT, engine

    reg = load_registry(ROOT)
    dataset = "detroit_dhd_covid_archive_daily"
    inproc = engine(reg, tmp_path, dataset, COVID_ROWS)
    iso = engine(reg, tmp_path, dataset, COVID_ROWS)
    iso.isolation = Isolation()
    args: dict[str, Any] = {"period": {"start": "2021-01-03", "end": "2021-01-12"}}
    want = inproc.query_metric(dataset, "daily_covid19_cases", **args)
    assert iso.query_metric(dataset, "daily_covid19_cases", **args) == want and want["rows"]
    assert [s.kind for s in iso._sources] == ["live"] and iso._sources[0].path is None

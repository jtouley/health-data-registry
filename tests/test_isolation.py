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
    """Remembers every child it started and how it ended, to check each one is gone when run() returns."""

    procs: list[Any]
    ends: list[tuple[int | None, bool]]

    def _reap(self, proc: Any, target: Any = None) -> tuple[int | None, bool]:
        self.__dict__.setdefault("procs", []).append(proc)
        end = super()._reap(proc, target)
        self.__dict__.setdefault("ends", []).append(end)
        return end


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
    for bad, want in (
        ('CREATE TABLE "snap_toy_cases".x AS SELECT 1', "query_failed"),
        ("SELECT * FROM read_csv('/etc/passwd')", "not_allowed"),
    ):
        status, code, message = execute(Job(src, sql=bad))
        assert status == "refused" and code == want and message.endswith(isolation.FAILED_NEXT)
        assert "Exception" not in message and "Error" not in message  # plain words, never the engine's type


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
        (KeyError("secret path"), "query_failed", f"{isolation.FAILED} {isolation.FAILED_NEXT}"),
        (
            duckdb.BinderException("x"),
            "query_failed",
            "The query does not fit the tables' columns or types. No numbers are shown. "
            + isolation.FAILED_NEXT,
        ),
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

        @staticmethod
        def getrlimit(which: str) -> tuple[int, int]:
            return next(v for w, v in reversed(calls) if w == which)

    monkeypatch.setitem(sys.modules, "resource", Resource)
    assert isolation.limit_child(1234) is LINUX
    assert calls == [("fsize", (0, 0)), ("core", (0, 0)), ("as", (1234, 1234))]
    calls.clear()
    assert isolation.limit_child(None) is False and [c[0] for c in calls] == ["fsize", "core"]

    class Lower(Resource):  # a container hard limit below the one asked for: set, but not what was asked
        @staticmethod
        def getrlimit(which: str) -> tuple[int, int]:
            return (1000, 1000)

    monkeypatch.setitem(sys.modules, "resource", Lower)
    assert isolation.limit_child(1234) is False

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
    assert status == {"poisoned": False, "memory_limit_mb": 4096, "memory_limit_enforced": None,
                      "start_method": isolation.start_method()}  # fmt: skip
    # Measured by a child, not assumed from the platform: the warm start runs one tiny query, once.
    assert iso.measure() is LINUX and iso.status()["memory_limit_enforced"] is LINUX
    assert iso.measure() is LINUX


# ------------------------------------------------------------------------------- the child process


def test_a_query_runs_in_a_child_and_the_child_is_gone_after(lake: Path) -> None:
    iso = Recording()
    cols, rows = iso.run(Job(sources(lake), sql="SELECT count(*) AS n FROM toy_cases"))
    assert (cols, rows) == (["n"], [[6]])
    assert len(iso.procs) == 1 and iso.ends == [(0, False)]  # exited by itself, reaped
    with pytest.raises(ValueError, match="closed"):  # and closed: its sentinel fd is released now
        iso.procs[0].is_alive()


def test_an_uninterruptible_call_is_killed_at_the_deadline(lake: Path) -> None:
    """A single long function call ignores con.interrupt(); the child is killed at the deadline anyway."""
    iso = Recording(memory_mb=16_384)  # memory is not what stops it here
    start = time.monotonic()
    with pytest.raises(IsolationRefused) as e:
        iso.run(Job(sources(lake), sql=HUGE, timeout_s=0.3))
    took = time.monotonic() - start
    assert e.value.code == "too_slow" and e.value.message == TOO_SLOW
    assert took < 0.3 + isolation.KILL_MARGIN_S + 1.0  # the query alone runs for several seconds
    assert iso.ends[0][0] == -9  # SIGKILLed, and reaped


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

    class Pipe:
        def __init__(self, error: str = "bad message length", ready: bool = True) -> None:
            self.error, self.ready = error, ready

        def poll(self, _: float) -> bool:
            return self.ready

        def recv_bytes(self, _: int) -> bytes:
            raise OSError(self.error)  # "bad message length": what Connection.recv_bytes(maxlength) raises

    class Proc:
        killed = False

        def kill(self) -> None:
            Proc.killed = True

    with pytest.raises(IsolationRefused) as e2:
        iso._read(Pipe(), Proc(), 1.0, 10, isolation.threading.Event())
    assert e2.value.code == "too_large" and Proc.killed
    # Killed part-way through a reply, or died mid-reply: not "too large"; _why says which it was.
    Proc.killed = False
    killed = isolation.threading.Event()
    assert iso._read(Pipe("got end of file during message"), Proc(), 1.0, 10, killed) is None
    assert not Proc.killed and not killed.is_set()
    killed.set()
    assert iso._read(Pipe(), Proc(), 1.0, 10, killed) is None and not Proc.killed
    # A wait that runs out is a timeout before any cleanup kill happens.
    late = isolation.threading.Event()
    assert iso._read(Pipe(ready=False), Proc(), 1.0, 10, late) is None and late.is_set()


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
    assert iso.ends[0][0] == -9  # gone (killed and reaped) before the next request starts
    out = eng.query_metric("toy_cases", "case_count", filters={"condition": "Legionellosis", "year": 2023})
    assert out["rows"] == [[710, 0]]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
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

    class Poisoned(Isolation):
        def run(self, job: Job, timeout_s: float | None = None) -> tuple[list[str], list[list[Any]]]:
            raise IsolationRefused("busy", POISONED)

    eng = Engine(registry=registry, lake=lake, isolation=Poisoned())
    with pytest.raises(QueryRefused) as e:  # a poisoned engine is said, not read as "no values"
        eng.loaded_values("toy_cases", "year")
    assert (e.value.code, str(e.value)) == ("busy", POISONED)


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
    import signal

    stopped = ("query_failed", isolation.STOPPED)
    memory = ("too_much_memory", TOO_MUCH_MEMORY)
    assert Isolation._why(-9, killed=True) == ("too_slow", TOO_SLOW)
    assert Isolation._why(-signal.SIGKILL, killed=False) == memory  # the kernel's out-of-memory kill
    assert Isolation._why(-signal.SIGABRT, killed=False) == memory  # an abort on a refused allocation
    assert Isolation._why(-signal.SIGKILL, killed=False, forced=True) == stopped  # the server's own reap kill
    for sig in ("SIGSEGV", "SIGXFSZ", "SIGBUS", "SIGTERM"):  # a crash is never "needed more memory"
        if hasattr(signal, sig):
            assert Isolation._why(-int(getattr(signal, sig)), killed=False) == stopped
    assert Isolation._why(1, killed=False) == stopped
    assert Isolation._why(None, killed=False) == stopped
    with pytest.raises(IsolationRefused) as e:
        Isolation().run(Job(()), timeout_s=0)
    assert e.value.code == "too_slow"


def test_a_wait_that_runs_out_is_a_timeout_even_before_the_timer_kills(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #92 review: when poll() gave up before the kill timer ran, the reap's SIGKILL read as out of memory."""

    class Never:  # the race: the timer thread hasn't run when poll returns
        def __init__(self, *_: Any) -> None:
            self.daemon = True

        def start(self) -> None:
            return None

        def cancel(self) -> None:
            return None

    monkeypatch.setattr(isolation.threading, "Timer", Never)
    iso = Recording(memory_mb=16_384)
    with pytest.raises(IsolationRefused) as e:
        iso.run(Job(sources(lake), sql=HUGE, timeout_s=0.2))
    assert (e.value.code, e.value.message) == ("too_slow", TOO_SLOW)
    assert iso.ends[0] == (-9, True)  # the cleanup kill, not the memory limit


def test_a_child_that_cant_start_closes_its_pipe_and_says_so_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    import multiprocessing

    pipes: list[Any] = []

    class Ctx:
        def Pipe(self, duplex: bool) -> Any:  # noqa: N802 - multiprocessing's own name
            pipes.extend(multiprocessing.Pipe(duplex=duplex))
            return pipes[0], pipes[1]

        def Process(self, **_: Any) -> Any:  # noqa: N802 - multiprocessing's own name
            class P:
                def start(self) -> None:
                    raise OSError(11, "Resource temporarily unavailable")  # EAGAIN from fork

            return P()

    iso = Isolation()
    iso._ctx = Ctx()
    with pytest.raises(IsolationRefused) as e:
        iso.run(Job((), sql="SELECT 1"))
    assert (e.value.code, e.value.message) == ("query_failed", isolation.NOT_STARTED)
    assert all(p.closed for p in pipes) and len(pipes) == 2

    class NoPipes(Ctx):
        def Pipe(self, duplex: bool) -> Any:  # noqa: N802 - multiprocessing's own name
            raise OSError(24, "Too many open files")

    iso._ctx = NoPipes()
    with pytest.raises(IsolationRefused) as e:
        iso.run(Job((), sql="SELECT 1"))
    assert (e.value.code, e.value.message) == ("query_failed", isolation.NOT_STARTED)


@pytest.mark.parametrize(
    "reply",
    [
        b"not json",
        b'{"a": 1}',
        b'["ok", [], [], "yes"]',
        b'["ok"]',
        b'["ok", [], [{"$t": "pickle", "v": 1}]]',
        b'["ok", [], [{"$t": "dict", "v": [[[[1]], 1]]}]]',
        b'["ok", 5, []]',
        b"[" * 100_000,
    ],
)
def test_a_reply_that_cant_be_decoded_is_refused_in_plain_words(reply: bytes) -> None:
    class Garbled(Isolation):
        def _read(self, *_: Any) -> bytes | None:
            return reply

    with pytest.raises(IsolationRefused) as e:
        Garbled().run(Job((), sql="SELECT 1"))
    assert (e.value.code, e.value.message) == ("query_failed", isolation.UNREADABLE)


def test_the_child_keeps_no_secret_and_writes_nothing_to_a_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = {"HOME": "/home/x", "PATH": "/bin", "HF_TOKEN": "secret", "HDR_SIGNING_KEY": "secret"}
    monkeypatch.setattr(isolation.os, "environ", fake)
    isolation.scrub_environment()
    assert fake == {"HOME": "/home/x", "PATH": "/bin"}


def _write_after_quiet(path: str) -> None:  # runs in a spawned child
    import os
    import resource

    isolation.quiet_output()
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    os.write(2, b"a library writing to stderr")
    print("and to stdout")
    try:
        with open(path, "w") as f:  # noqa: PTH123 - the plain call a library would make
            f.write("x")
    except OSError:
        os._exit(3)  # refused with an error (EFBIG), not killed by SIGXFSZ
    os._exit(0)


@pytest.mark.skipif(not LINUX, reason="RLIMIT_FSIZE and SIGXFSZ behave this way on Linux")
def test_output_under_rlimit_fsize_never_kills_the_child(tmp_path: Path) -> None:
    """PR #92 review: inherited stderr pointing at a log file would SIGXFSZ the child under RLIMIT_FSIZE 0."""
    import multiprocessing

    p = multiprocessing.get_context("spawn").Process(target=_write_after_quiet, args=(str(tmp_path / "f"),))
    p.start()
    p.join(60)
    assert p.exitcode == 3


def test_only_the_tables_a_query_reads_go_to_the_child(lake: Path, registry: Registry) -> None:
    """PR #92 review: every source went to every child; a broken one elsewhere failed every query."""
    jobs: list[Job] = []

    class Watching(Isolation):
        def run(self, job: Job, timeout_s: float | None = None) -> tuple[list[str], list[list[Any]]]:
            jobs.append(job)
            return super().run(job, timeout_s)

    eng = Engine(registry=registry, lake=lake, isolation=Watching())
    eng.connect()
    eng._sources.append(Source("live", "broken_elsewhere"))  # would fail build_connection if it were sent
    out = eng.query_metric("toy_cases", "case_count", filters={"condition": "Legionellosis", "year": 2023})
    assert out["rows"] == [[710, 0]]
    assert eng.query_sql("SELECT count(*) AS n FROM toy_cases")["rows"] == [[6]]
    assert eng.distinct_values("toy_cases", "year") == [2022, 2023, 2024]
    assert eng.loaded_values("toy_cases", "year") == [2022, 2023, 2024]
    assert [[s.dataset_id for s in j.sources] for j in jobs] == [["toy_cases"]] * 4


def test_the_child_opens_duckdb_with_its_thread_and_memory_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #92 review: a connection opened at DuckDB's defaults starts a thread per CPU before harden() runs."""
    import duckdb

    seen: list[dict[str, str]] = []
    real = duckdb.connect

    def connect(database: str, config: dict[str, str] | None = None) -> Any:
        seen.append(dict(config or {}))
        return real(database, config=config or {})

    monkeypatch.setattr(duckdb, "connect", connect)
    con = isolation.build_connection((), "1GB")
    assert seen == [{"threads": "2", "memory_limit": "1GB"}]
    assert con.execute("SELECT current_setting('threads')").fetchone() == (2,)


@pytest.mark.skipif(not hasattr(isolation.os, "pidfd_open"), reason="pidfd is Linux 5.3+")
def test_the_kill_goes_through_a_pidfd_so_it_cant_reach_a_reused_pid() -> None:
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603 - our own Python
    target = isolation.Target(child)
    assert target.fd is not None
    target.kill()
    assert child.wait(10) == -9
    target.kill()  # gone: a no-op, never an error
    target.close()
    target.close()
    assert target.fd is None

    class NoPid:
        killed = False

        def kill(self) -> None:
            NoPid.killed = True

    fallback = isolation.Target(NoPid())
    assert fallback.fd is None
    fallback.kill()
    assert NoPid.killed


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

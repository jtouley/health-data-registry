"""The query child's own steps, run in this process: its connection, its reply, its limits and its quiet output.

test_isolation.py runs them in a real child, which proves they work together but hides each step from a test: the
child drops the parent's environment first, so a change to one of these steps is not seen there (hard rule 13;
issue #212). Here each step runs on its own, with the parts that would change this process (its environment, its
file descriptors, its limits) replaced by recorders.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import isolation, sql_guard
from health_data_registry.isolation import (
    MIB,
    REPLY_TOO_LARGE,
    TOO_MUCH_MEMORY,
    TOO_SLOW,
    Job,
    Source,
    decode_reply,
    encode,
    execute,
)

# ------------------------------------------------------------------------------------------ the connection


def _duckdb_file(path: Path, table: str, n: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(f'CREATE TABLE "{table}" AS SELECT range AS i FROM range({n})')
    con.close()
    return str(path)


def _parquet_file(path: Path, n: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    lit = str(path).replace("'", "''")
    duckdb.connect(":memory:").execute(
        f"COPY (SELECT range AS i FROM range({n})) TO '{lit}' (FORMAT parquet)"
    )
    return str(path)


def test_identifiers_are_quoted_with_their_quotes_doubled() -> None:
    assert isolation._q('toy"cases') == '"toy""cases"'


def test_snapshots_with_a_quote_in_their_path_attach_side_by_side(tmp_path: Path) -> None:
    a = _duckdb_file(tmp_path / "o'neil" / "a.duckdb", "a", 2)
    b = _duckdb_file(tmp_path / "b.duckdb", "b", 3)
    con = isolation.build_connection([Source("attach", "a", path=a), Source("attach", "b", path=b)], "1GB")
    assert con.execute('SELECT count(*) FROM "a"').fetchone() == (2,)
    assert con.execute('SELECT count(*) FROM "b"').fetchone() == (3,)


def test_a_hub_parquet_file_is_readable_after_hardening_and_nothing_else_is(tmp_path: Path) -> None:
    path = _parquet_file(tmp_path / "o'neil" / "data.parquet", 4)
    other = _parquet_file(tmp_path / "other.parquet", 1)
    con = isolation.build_connection([Source("parquet", "hub_cases", path=path)], "1GB")
    assert con.execute('SELECT count(*) FROM "hub_cases"').fetchone() == (4,)
    assert con.execute("SELECT current_setting('memory_limit')").fetchone() == (
        duckdb.connect(":memory:", config={"memory_limit": "1GB"})
        .execute("SELECT current_setting('memory_limit')")
        .fetchone()
    )
    with pytest.raises(duckdb.Error):
        con.execute(f"SELECT * FROM read_parquet('{other}')").fetchall()  # only the catalogued file


def test_a_live_table_is_built_from_the_records_it_was_given(tmp_path: Path) -> None:
    from health_data_registry.live import load_live
    from health_data_registry.registry import load_registry
    from tests.test_michigan_models import COVID_ROWS, ROOT

    dataset = "detroit_dhd_covid_archive_daily"
    ds = load_registry(ROOT).get(dataset)
    plain = duckdb.connect(":memory:")
    load_live(plain, ds, list(COVID_ROWS))
    want = plain.execute(f'SELECT count(*) FROM "{dataset}"').fetchone()
    assert want is not None and want[0] > 0
    con = isolation.build_connection([Source("live", dataset, ds=ds, records=list(COVID_ROWS))], "1GB")
    assert con.execute(f'SELECT count(*) FROM "{dataset}"').fetchone() == want


# ------------------------------------------------------------------------------------------ execute


def test_execute_passes_the_jobs_memory_limit_and_timeout_on(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    real_build = isolation.build_connection

    def build(sources: Any, memory_limit: str = "unset") -> Any:
        seen["memory_limit"] = memory_limit
        return real_build(sources, memory_limit)

    def run_guarded(con: Any, q: Any, **kw: Any) -> tuple[list[str], list[tuple[int]]]:
        seen.update(kw)
        return ["x"], [(1,)]

    monkeypatch.setattr(isolation, "build_connection", build)
    monkeypatch.setattr(sql_guard, "run_guarded", run_guarded)
    q = sql_guard.guard_sql("SELECT 1 AS x", allowed_tables={}, max_rows=1)
    assert execute(Job((), guarded=q, timeout_s=7.5, memory_limit="1GB")) == ("ok", ["x"], [[1]])
    assert seen == {"memory_limit": "1GB", "timeout_s": 7.5}


def test_memory_and_interrupts_are_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    for exc, code, words in (
        (MemoryError(), "too_much_memory", TOO_MUCH_MEMORY),
        (duckdb.InterruptException("x"), "too_slow", TOO_SLOW),
    ):

        def boom(*_: Any, e: BaseException = exc) -> Any:
            raise e

        monkeypatch.setattr(isolation, "build_connection", boom)
        assert execute(Job(())) == ("refused", code, words)


# ------------------------------------------------------------------------------------------ the reply


def test_a_reply_is_compact_json_with_the_memory_flag_only_when_known() -> None:
    assert encode(("ok", ["n"], [[1]])) == b'["ok",["n"],[[1]]]'
    assert encode(("ok", ["n"], [[1]]), True) == b'["ok",["n"],[[1]],true]'
    assert encode(("ok", ["n"], [[1]]), False) == b'["ok",["n"],[[1]],false]'


class _Quiet:
    """Stands in for the steps that would change this test process: env, file descriptors, malloc, limits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, enforced: bool = True) -> None:
        self.limits: list[int | None] = []
        monkeypatch.setattr(isolation, "scrub_environment", lambda: None)
        monkeypatch.setattr(isolation, "quiet_output", lambda: None)
        monkeypatch.setattr(isolation, "limit_arenas", lambda: None)

        def limit_child(memory_bytes: int | None) -> bool:
            self.limits.append(memory_bytes)
            return enforced

        monkeypatch.setattr(isolation, "limit_child", limit_child)


def _child(job: Job, memory_bytes: int | None, result_bytes: int) -> tuple[tuple[str, Any, Any], bool | None]:
    parent, child = multiprocessing.Pipe(duplex=False)
    isolation.child_main(job, child, memory_bytes, result_bytes)
    return decode_reply(parent.recv_bytes())


def test_the_child_sends_its_answer_with_the_memory_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    quiet = _Quiet(monkeypatch)
    jobs: list[Job] = []

    def run(job: Job) -> tuple[str, Any, Any]:
        jobs.append(job)
        return "ok", ["n"], [[1]]

    monkeypatch.setattr(isolation, "execute", run)
    job = Job((), sql="SELECT 1 AS n")
    assert _child(job, 1234, 10 * MIB) == (("ok", ["n"], [[1]]), True)
    assert jobs == [job] and quiet.limits == [1234]


def test_the_child_answers_running_out_of_memory_as_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _Quiet(monkeypatch)

    def run(job: Job) -> tuple[str, Any, Any]:
        raise MemoryError

    monkeypatch.setattr(isolation, "execute", run)
    assert _child(Job(()), 1234, 10 * MIB) == (("refused", "too_much_memory", TOO_MUCH_MEMORY), True)


def test_the_child_sends_a_reply_of_exactly_the_cap_and_refuses_one_byte_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Quiet(monkeypatch)
    reply = ("ok", ["n"], [["x" * 40]])
    monkeypatch.setattr(isolation, "execute", lambda job: reply)
    size = len(encode(reply, True))
    assert _child(Job(()), 1, size) == ((reply[0], reply[1], reply[2]), True)
    over = _child(Job(()), 1, size - 1)
    assert over == (("refused", "too_large", REPLY_TOO_LARGE.format(mb=(size - 1) // MIB)), True)


def test_the_cap_in_the_refusal_is_in_whole_mib(monkeypatch: pytest.MonkeyPatch) -> None:
    _Quiet(monkeypatch)
    monkeypatch.setattr(isolation, "execute", lambda job: ("ok", ["n"], [["x" * (2 * MIB)]]))
    reply, _ = _child(Job(()), 1, MIB + MIB // 2)
    assert reply == ("refused", "too_large", REPLY_TOO_LARGE.format(mb=1))


# ------------------------------------------------------------------------------------------ the limits


class _Resource:
    RLIMIT_AS, RLIMIT_FSIZE = "as", "fsize"  # no RLIMIT_CORE: some platforms lack one
    calls: list[tuple[str, tuple[int, int]]] = []

    @classmethod
    def setrlimit(cls, which: str, value: tuple[int, int]) -> None:
        cls.calls.append((which, value))
        if which == "fsize":
            raise OSError("not permitted here")

    @staticmethod
    def getrlimit(which: str) -> tuple[int, int]:
        return (1234, 99_999)  # soft limit as asked; the hard limit stays higher


def test_limits_skip_what_the_platform_lacks_and_read_back_the_soft_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Resource.calls = []
    monkeypatch.setitem(sys.modules, "resource", _Resource)
    assert isolation.limit_child(1234) is sys.platform.startswith("linux")
    assert _Resource.calls == [("fsize", (0, 0)), ("as", (1234, 1234))]


def test_quiet_output_ignores_sigxfsz_and_points_both_streams_at_devnull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(isolation.signal, "signal", lambda *a: calls.append(("signal", *a)))
    monkeypatch.setattr(isolation.os, "open", lambda *a: calls.append(("open", *a)) or 99)
    monkeypatch.setattr(isolation.os, "dup2", lambda *a: calls.append(("dup2", *a)))
    monkeypatch.setattr(isolation.os, "close", lambda *a: calls.append(("close", *a)))
    isolation.quiet_output()
    want: list[tuple[Any, ...]] = []
    if hasattr(isolation.signal, "SIGXFSZ"):
        want.append(("signal", isolation.signal.SIGXFSZ, isolation.signal.SIG_IGN))
    want += [
        ("open", isolation.os.devnull, isolation.os.O_WRONLY),
        ("dup2", 99, 1),
        ("dup2", 99, 2),
        ("close", 99),
    ]
    assert calls == want


def test_malloc_arenas_are_capped_on_linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    calls: list[tuple[int, int]] = []

    class Libc:
        def mallopt(self, option: int, value: int) -> None:
            calls.append((option, value))

    monkeypatch.setattr(ctypes, "CDLL", lambda name: Libc())
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    isolation.limit_arenas()
    assert calls == [(-8, 2)]  # M_ARENA_MAX, two arenas

    def no_libc(name: Any) -> Any:
        raise OSError("musl")

    monkeypatch.setattr(ctypes, "CDLL", no_libc)
    isolation.limit_arenas()  # not glibc: nothing to cap, and no error
    monkeypatch.setattr(ctypes, "CDLL", lambda name: Libc())
    monkeypatch.setattr(isolation.sys, "platform", "darwin")
    isolation.limit_arenas()
    assert calls == [(-8, 2)]


def test_the_reply_codec_is_plain_json() -> None:
    assert json.loads(encode(("refused", "too_slow", TOO_SLOW))) == ["refused", "too_slow", TOO_SLOW]

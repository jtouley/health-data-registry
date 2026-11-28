"""Run each query in a child process the server can kill: an OS memory limit, a hard deadline, a capped reply.

The hosted server (hosted.py) runs every query it is asked for, query_sql and compiled metric SQL alike, here
rather than on its own DuckDB connection, because a thread can't be stopped from outside: DuckDB's
`con.interrupt()` can't stop one long function call (a huge `lpad`, `repeat`), DuckDB's memory_limit doesn't count
every allocation, and a worker the server gave up on could keep running after the next question started.

- **A fresh child per query.** `Isolation.run` starts a child (from a forkserver with DuckDB, sqlglot and the CLI
  already imported, so a query costs 40-80 ms more than in-process on Linux, measured on the synthetic lake,
  rather than an interpreter start-up), hands it a `Job`: only the tables that query reads, and the SQL. The
  child clears its environment (no server secret reaches it), sends its own stdout and stderr to /dev/null, opens
  its own in-memory DuckDB connection with 2 threads from the start, attaches lake snapshots READ_ONLY and reads
  hub Parquet files through views, loads restricted live tables into memory from the records the parent holds
  (never to disk), locks the connection down (sql_guard.harden) and runs the query. It shares no memory with the
  server, and it never starts a process of its own (DuckDB has no way to, and the guard allows one SELECT only).
- **An OS memory limit.** Before it touches DuckDB the child sets RLIMIT_AS (HDR_QUERY_MEMORY_MB, default 4,096
  MiB of address space: DuckDB's own 2 GB memory_limit plus its baseline), so an allocation past the limit fails
  inside the child (DuckDB reports out of memory, or the child aborts) and the server says so in plain words; the
  server itself never runs out. The child checks that the limit took effect and says so in its reply (/healthz
  shows it). RLIMIT_FSIZE 0 means the child can't grow a file by one byte (it can still open one: the views read
  hub files the server made read-only after their sha256 check, and the guard allows SELECT only), and
  RLIMIT_CORE 0 means a crash leaves no core dump with data in it.
- **A hard kill at the deadline.** The parent waits for the reply until the call's deadline (deadline.py) or the
  job's own limit, whichever comes first, then SIGKILLs the child, through a pidfd on Linux so the signal can
  reach only that child, never a process that later got its PID. A kill stops anything: a function call that
  ignores interrupts, a loop in a C library. Nothing the killed child did reaches the server: it held only
  read-only files and its own pipe, and a reply that arrives after the kill is never read. A wait that runs out
  is a timeout, whatever the kill shows; a child that died of SIGKILL or SIGABRT the server didn't send ran out
  of memory; any other death (a crash) is reported as the engine stopping.
- **A capped reply.** The child sends one message over a one-way pipe: JSON with tagged dates, decimals and the
  like (never pickle, so a child can't make the server run code), at most HDR_QUERY_RESULT_MB (default 64 MiB).
  The server enforces the cap itself when it reads (recv_bytes' maxlength), so a child can't exhaust its memory.
- **Poisoned until reaped.** A child that doesn't exit after SIGKILL (stuck in the kernel) is kept on a list, and
  while any is alive the engine is poisoned: `poisoned()` is true, new queries are refused, and hosted.py says so in
  the BUSY refusal and on /healthz.

Platforms: Linux (the Space) gets all of the above. On macOS the start method is also forkserver but RLIMIT_AS is
not enforced by the kernel, and on Windows there is no `resource` module and children are spawned (each start
loads Python and DuckDB, so it is slower). There the deadline kill and the reply cap still hold, and memory is
bounded only by DuckDB's memory_limit, which doesn't count every allocation: a weaker guarantee, stated by
`Isolation.memory_enforced` and on /healthz.

The local stdio server keeps running queries in-process (Engine without an Isolation): it serves one clinician on
their own computer, where a runaway query costs only that person's session and a restart of Claude fixes it, and
most clinicians run macOS or Windows, where the child would add those 40-80 ms (far more where children are spawned)
without the memory limit. Its own queries stay under sql_guard's interrupt and DuckDB's memory_limit.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import decimal
import json
import multiprocessing
import os
import signal
import sys
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

    from health_data_registry.registry import Dataset
    from health_data_registry.sql_guard import GuardedQuery

MIB = 1024 * 1024
DEFAULT_MEMORY_MB = 4096  # address space: DuckDB's 2 GB memory_limit, its baseline (~1-1.5 GiB) and headroom
MIN_MEMORY_MB = 1024  # under this DuckDB can't even start reliably
DEFAULT_RESULT_MB = 64  # MAX_ROWS rows of even very wide tables fit with room to spare
DEFAULT_JOB_TIMEOUT_S = 200.0  # outside a call deadline (the CLI, tests); hosted calls use their own budget
KILL_MARGIN_S = 1.0  # past a job's own timeout before the kill, so a guarded query can say it timed out
KILL_WAIT_S = 2.0  # after SIGKILL, how long to wait for the child to be reaped before calling it stuck
TAG = "$t"  # the key that marks a tagged value in a reply
# Imported once in the forkserver, so a child starts with them loaded (sqlglot alone takes ~150 ms to import).
# The CLI too: multiprocessing re-runs the parent's main script in every child (Python 3.12 can't preload it),
# and the server's main script is the `health-data-registry` entry point, which imports the CLI.
PRELOAD = ["duckdb", "health_data_registry.cli", "health_data_registry.isolation", "health_data_registry.sql_guard",
           "health_data_registry.live", "health_data_registry.registry"]  # fmt: skip

POISONED = (
    "The hosted server is still stopping an earlier question that ran too long, so it isn't taking new questions "
    "yet and no numbers are shown. Try again in a few minutes; if this keeps happening, the server needs a "
    "restart, and its health page says so."
)
TOO_MUCH_MEMORY = (
    "This question needed more memory than one question may use, so it was stopped and no numbers are shown. "
    "Ask again for fewer columns, fewer groups or a shorter span of years, and avoid padding or repeating text."
)
TOO_SLOW = (
    "This question took longer than one question may run, so it was stopped and no numbers are shown. Narrow it: "
    "one condition, fewer places or fewer years."
)
REPLY_TOO_LARGE = (
    "This answer is larger than one reply may be (over {mb:,} MiB), so nothing was sent. Ask again for fewer "
    "columns or fewer rows."
)
STOPPED = (
    "The data engine stopped unexpectedly while answering, so no numbers are shown. Try again; if it keeps "
    "happening, narrow the question or run the Something's wrong helper."
)
NOT_STARTED = (
    "The hosted server couldn't start the data engine for this question, so no numbers are shown. Try again in a "
    "minute; if it keeps happening, run the Something's wrong helper."
)
UNREADABLE = (
    "The data engine's answer to this question couldn't be read, so no numbers are shown. Try again; if it keeps "
    "happening, run the Something's wrong helper."
)
FAILED = "The data engine could not run this query on these tables, so no numbers are shown."
FAILED_NEXT = (
    "Check the columns with describe_dataset and ask again; if it keeps happening, run the Something's "
    "wrong helper."
)
# What the child keeps of the server's environment: what DuckDB and Python need, never a token or a secret.
CHILD_ENV = ("HOME", "TMPDIR", "TMP", "TEMP", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT")


class IsolationRefused(Exception):
    """The query in the child gave no answer; `code` is for programs, the message is plain words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------------------------------ the job


@dataclass(frozen=True)
class Source:
    """One table the child sets up: a lake snapshot (attach), a hub Parquet file (parquet) or a live table."""

    kind: str  # "attach" | "parquet" | "live"
    dataset_id: str
    path: str | None = None
    ds: Dataset | None = None  # live only: the contract that normalises the records
    records: list[dict[str, Any]] | None = None  # live only: held in memory, never written


@dataclass(frozen=True)
class Job:
    """What one child runs: guarded SQL (run through sql_guard.run_guarded) or trusted SQL with bound params."""

    sources: tuple[Source, ...]
    sql: str = ""
    params: Sequence[Any] = ()
    limit: int | None = None  # rows fetched at most; None fetches all
    guarded: GuardedQuery | None = None
    timeout_s: float = DEFAULT_JOB_TIMEOUT_S
    memory_limit: str = "2GB"  # DuckDB's own limit inside the child (sql_guard.harden)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def build_connection(sources: Sequence[Source], memory_limit: str = "2GB") -> Any:
    """A hardened in-memory DuckDB connection with every source attached read-only (the child's only state)."""
    import duckdb

    from health_data_registry.live import load_live
    from health_data_registry.sql_guard import DEFAULT_THREADS, harden

    # Threads and memory are set as the connection opens, so DuckDB never starts one thread per CPU (each with its
    # own stack and allocator arena) and uses up RLIMIT_AS before a query runs; harden() sets them again.
    con = duckdb.connect(":memory:", config={"threads": str(DEFAULT_THREADS), "memory_limit": memory_limit})
    parquet: list[str] = []
    for s in sources:
        if s.kind == "attach" and s.path is not None:
            lit = s.path.replace("'", "''")
            alias = _q(f"snap_{s.dataset_id}")
            con.execute(f"ATTACH '{lit}' AS {alias} (READ_ONLY)")
            con.execute(f"CREATE VIEW {_q(s.dataset_id)} AS SELECT * FROM {alias}.{_q(s.dataset_id)}")  # noqa: S608 - quoted contract identifiers
        elif s.kind == "parquet" and s.path is not None:
            lit = s.path.replace("'", "''")
            con.execute(f"CREATE VIEW {_q(s.dataset_id)} AS SELECT * FROM read_parquet('{lit}')")  # noqa: S608 - quoted identifier, verified cache path
            parquet.append(s.path)
        elif s.kind == "live" and s.ds is not None:
            load_live(con, s.ds, list(s.records or []))
        else:
            raise ValueError(f"unknown source kind {s.kind!r} for {s.dataset_id}")
    harden(con, allowed_paths=parquet, memory_limit=memory_limit)
    return con


def execute(job: Job) -> tuple[str, Any, Any]:
    """Run one job on a fresh connection: ("ok", columns, rows) or ("refused", code, message). Never raises."""
    import duckdb

    from health_data_registry.sql_guard import _COULD_NOT_RUN, SqlRefused, run_guarded

    try:
        con = build_connection(job.sources, job.memory_limit)
        try:
            if job.guarded is not None:
                cols, rows = run_guarded(con, job.guarded, timeout_s=job.timeout_s)
            else:
                cur = con.execute(job.sql, list(job.params))
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchall() if job.limit is None else cur.fetchmany(job.limit)
        finally:
            con.close()
    except SqlRefused as e:
        return "refused", e.code, e.message
    except (MemoryError, duckdb.OutOfMemoryException):
        return "refused", "too_much_memory", TOO_MUCH_MEMORY
    except duckdb.InterruptException:
        return "refused", "too_slow", TOO_SLOW
    except Exception as e:  # noqa: BLE001 - the server answers in plain words, never the engine's own text
        for kind, code, words in _COULD_NOT_RUN:
            if isinstance(e, kind):
                return "refused", code, f"{words} No numbers are shown. {FAILED_NEXT}"
        return "refused", "query_failed", f"{FAILED} {FAILED_NEXT}"
    return "ok", cols, [list(r) for r in rows]


# -------------------------------------------------------------------------------------- the reply codec


def _tag(kind: str, value: Any) -> dict[str, Any]:
    return {TAG: kind, "v": value}


def _enc(v: Any) -> Any:
    """A JSON-safe form of one DuckDB value; anything not plain JSON is tagged so it comes back as itself."""
    if v is None or isinstance(v, bool | int | float | str):
        return v
    if isinstance(v, dt.datetime):
        return _tag("datetime", v.isoformat())
    if isinstance(v, dt.date):
        return _tag("date", v.isoformat())
    if isinstance(v, dt.time):
        return _tag("time", v.isoformat())
    if isinstance(v, dt.timedelta):
        return _tag("timedelta", [v.days, v.seconds, v.microseconds])
    if isinstance(v, decimal.Decimal):
        return _tag("decimal", str(v))
    if isinstance(v, uuid.UUID):
        return _tag("uuid", str(v))
    if isinstance(v, bytes | bytearray | memoryview):
        return _tag("bytes", base64.b64encode(bytes(v)).decode("ascii"))
    if isinstance(v, list | tuple):
        return [_enc(x) for x in v]
    if isinstance(v, dict):
        return _tag("dict", [[_enc(k), _enc(x)] for k, x in v.items()])
    return str(v)  # an engine type with no JSON form: its text, as the CSV would show it


def _dec(obj: dict[str, Any]) -> Any:
    kind = obj.get(TAG)
    if kind is None:
        return obj
    v = obj["v"]
    decoders: dict[str, Any] = {
        "datetime": dt.datetime.fromisoformat,
        "date": dt.date.fromisoformat,
        "time": dt.time.fromisoformat,
        "timedelta": lambda p: dt.timedelta(days=p[0], seconds=p[1], microseconds=p[2]),
        "decimal": decimal.Decimal,
        "uuid": uuid.UUID,
        "bytes": base64.b64decode,
        "dict": lambda pairs: {_hashable(k): x for k, x in pairs},
    }
    if kind not in decoders:
        raise ValueError(f"unknown tag {kind!r} in a query reply")
    return decoders[kind](v)


def _hashable(k: Any) -> Any:
    return tuple(k) if isinstance(k, list) else k


def encode(reply: tuple[str, Any, Any], enforced: bool | None = None) -> bytes:
    """One reply as JSON; `enforced` (did the child's memory limit take effect?) travels with it when given."""
    body = [reply[0], _enc(reply[1]), _enc(reply[2])]
    return json.dumps(body if enforced is None else [*body, bool(enforced)], separators=(",", ":")).encode()


def decode_reply(data: bytes) -> tuple[tuple[str, Any, Any], bool | None]:
    """A reply and whether the child's memory limit was enforced (None when the reply doesn't say)."""
    got = json.loads(data, object_hook=_dec)
    if not isinstance(got, list) or len(got) not in (3, 4):
        raise ValueError("a query reply is a list of 3 or 4 items")
    enforced = got[3] if len(got) == 4 else None
    if enforced is not None and not isinstance(enforced, bool):
        raise ValueError("a query reply's memory flag is true or false")
    return (str(got[0]), got[1], got[2]), enforced


def decode(data: bytes) -> tuple[str, Any, Any]:
    return decode_reply(data)[0]


# ------------------------------------------------------------------------------------------ the child


def limit_child(memory_bytes: int | None) -> bool:
    """Set the child's OS limits; True when the memory limit is enforced (Linux), False where it can't be."""
    try:
        import resource
    except ImportError:  # Windows
        return False
    for name, value in (("RLIMIT_FSIZE", 0), ("RLIMIT_CORE", 0)):
        limit = getattr(resource, name, None)
        if limit is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(limit, (value, value))
    if memory_bytes is None or not hasattr(resource, "RLIMIT_AS"):
        return False
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        took = resource.getrlimit(resource.RLIMIT_AS)[0] == memory_bytes  # measured, not assumed
    except (ValueError, OSError):  # macOS refuses or ignores it; a container's hard limit may be lower
        return False
    return took and sys.platform.startswith("linux")


def scrub_environment(keep: Sequence[str] = CHILD_ENV) -> None:
    """Drop every environment variable but `keep`: the forkserver inherited the server's, Space secrets included."""
    kept = {k: os.environ[k] for k in keep if k in os.environ}
    os.environ.clear()
    os.environ.update(kept)


def quiet_output() -> None:
    """Point stdout and stderr at /dev/null and ignore SIGXFSZ, before RLIMIT_FSIZE 0 applies.

    The child inherits the server's stderr, which may be a log file: a write there under RLIMIT_FSIZE 0 would kill
    the child with SIGXFSZ. /dev/null isn't a regular file, so the limit never applies to it, and with SIGXFSZ
    ignored any other write past the limit fails with an error instead of killing the child.
    """
    if hasattr(signal, "SIGXFSZ"):
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        for fd in (1, 2):
            os.dup2(null, fd)
    finally:
        os.close(null)


def limit_arenas(arenas: int = 2) -> None:
    """Cap glibc's malloc arenas (as MALLOC_ARENA_MAX would), which each reserve address space RLIMIT_AS counts."""
    if not sys.platform.startswith("linux"):
        return
    with contextlib.suppress(Exception):  # not glibc (musl), or no libc to load: nothing to cap
        import ctypes

        ctypes.CDLL(None).mallopt(-8, arenas)  # M_ARENA_MAX


def child_main(job: Job, conn: Connection, memory_bytes: int | None, result_bytes: int) -> None:
    """The child's whole life: no secrets, no output, limits, the query, one reply no larger than the cap."""
    scrub_environment()
    quiet_output()
    limit_arenas()
    enforced = limit_child(memory_bytes)
    try:
        payload = encode(execute(job), enforced)
    except MemoryError:
        payload = encode(("refused", "too_much_memory", TOO_MUCH_MEMORY), enforced)
    if len(payload) > result_bytes:
        payload = encode(("refused", "too_large", REPLY_TOO_LARGE.format(mb=result_bytes // MIB)), enforced)
    conn.send_bytes(payload)
    conn.close()


# ------------------------------------------------------------------------------------------- the parent


def _env_mb(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    value = int(raw) if raw.strip().isdigit() else 0
    if value < minimum:
        raise ValueError(
            f"{name} must be a whole number of MiB, at least {minimum:,}; got {raw!r}. Unset it to use {default:,}."
        )
    return value


def start_method() -> str:
    """forkserver where there is one (Linux, macOS): a clean, preloaded parent to fork from; else spawn."""
    return "forkserver" if "forkserver" in multiprocessing.get_all_start_methods() else "spawn"


@dataclass
class Isolation:
    """Runs jobs in killable child processes and remembers any child it could not reap (the poisoned state)."""

    memory_mb: int = field(
        default_factory=lambda: _env_mb("HDR_QUERY_MEMORY_MB", DEFAULT_MEMORY_MB, MIN_MEMORY_MB)
    )
    result_mb: int = field(default_factory=lambda: _env_mb("HDR_QUERY_RESULT_MB", DEFAULT_RESULT_MB, 1))
    method: str = field(default_factory=start_method)
    kill_wait_s: float = KILL_WAIT_S
    _unreaped: list[Any] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _ctx: Any = None
    _enforced: bool | None = None  # what the last child measured (limit_child); None before any child ran

    def context(self) -> Any:
        if self._ctx is None:
            ctx = multiprocessing.get_context(self.method)
            if self.method == "forkserver":
                ctx.set_forkserver_preload(PRELOAD)
            self._ctx = ctx
        return self._ctx

    @property
    def memory_enforced(self) -> bool | None:
        """Did the OS memory limit take effect in the last child? As that child measured it; None before any ran."""
        return self._enforced

    def measure(self) -> bool | None:
        """Run one tiny query (once, at warm start) so /healthz can say whether the memory limit is enforced."""
        if self._enforced is None and not self.poisoned():
            with contextlib.suppress(IsolationRefused):
                self.run(Job((), sql="SELECT 1"))
        return self._enforced

    def poisoned(self) -> bool:
        """True while a child that ignored SIGKILL is still alive; reaped children are forgotten."""
        with self._lock:
            self._unreaped = [p for p in self._unreaped if p.is_alive()]
            return bool(self._unreaped)

    def status(self) -> dict[str, Any]:
        return {
            "poisoned": self.poisoned(),
            "memory_limit_mb": self.memory_mb,
            "memory_limit_enforced": self.memory_enforced,  # null until a child has measured it
            "start_method": self.method,
        }

    def run(self, job: Job, timeout_s: float | None = None) -> tuple[list[str], list[list[Any]]]:
        """Run `job` in a child; (columns, rows), or IsolationRefused in plain words. Kills the child at the deadline.

        `timeout_s` is the time left in the call (deadline.remaining()); the job's own timeout caps it too.
        """
        if self.poisoned():
            raise IsolationRefused("busy", POISONED)
        # A guarded query interrupts itself at job.timeout_s and says so; the kill comes just after, for anything
        # an interrupt can't stop.
        wait = (
            job.timeout_s + KILL_MARGIN_S
            if timeout_s is None
            else min(job.timeout_s + KILL_MARGIN_S, timeout_s)
        )
        if wait <= 0:
            raise IsolationRefused("too_slow", TOO_SLOW)
        cap = self.result_mb * MIB
        reader, writer, proc = self._start(job, cap)
        target = Target(proc)
        killed = threading.Event()
        state = threading.Lock()
        done = False

        def at_deadline() -> None:
            with state:  # never after the reap below: the PID may belong to another process by then
                if done:
                    return
                killed.set()
                target.kill()

        # The kill happens at the deadline whatever the parent is doing (even mid-read of a stalled reply).
        killer = threading.Timer(wait, at_deadline)
        killer.daemon = True
        killer.start()
        try:
            data = self._read(reader, target, wait, cap, killed)
        finally:
            with state:
                done = True
            killer.cancel()
            reader.close()
            code, forced = self._reap(proc, target)
            target.close()
        if data is None or killed.is_set():  # a reply that raced the kill is never used
            raise IsolationRefused(*self._why(code, killed.is_set(), forced))
        try:
            (status, a, b), enforced = decode_reply(data)
            answer = ([str(c) for c in a], list(b)) if status == "ok" else None
        except Exception:  # noqa: BLE001 - the child is the untrusted side: any reply it can't decode is refused
            raise IsolationRefused("query_failed", UNREADABLE) from None
        if enforced is not None:
            self._enforced = enforced
        if answer is None:
            raise IsolationRefused(str(a), str(b))
        return answer

    def _start(self, job: Job, cap: int) -> tuple[Any, Any, Any]:
        """Start one child with its reply pipe; a failed start closes both ends and is refused in plain words."""
        try:
            ctx = self.context()
            reader, writer = ctx.Pipe(duplex=False)
        except Exception:  # noqa: BLE001 - out of descriptors, or no forkserver: one plain refusal
            raise IsolationRefused("query_failed", NOT_STARTED) from None
        try:
            args = (job, writer, self.memory_mb * MIB, cap)
            proc = ctx.Process(target=child_main, args=args, daemon=True, name="hdr-query")
            proc.start()
        except Exception:  # noqa: BLE001 - fork refused (EAGAIN, ENOMEM) or the forkserver gone
            reader.close()
            writer.close()
            raise IsolationRefused("query_failed", NOT_STARTED) from None
        writer.close()  # only the child writes: EOF here once it exits
        return reader, writer, proc

    @staticmethod
    def _why(code: int | None, killed: bool, forced: bool = False) -> tuple[str, str]:
        """Why a child gave no reply, in plain words: out of time, out of memory, or stopped."""
        if killed:
            return "too_slow", TOO_SLOW
        if not forced and code in MEMORY_DEATHS:
            # A SIGKILL or SIGABRT the server didn't send: the kernel's out-of-memory kill, or an abort on an
            # allocation RLIMIT_AS refused.
            return "too_much_memory", TOO_MUCH_MEMORY
        return "query_failed", STOPPED  # a crash (SIGSEGV and the like), an exit without a reply

    def _read(self, reader: Any, target: Any, wait: float, cap: int, killed: threading.Event) -> bytes | None:
        try:
            if not reader.poll(wait):
                killed.set()  # out of time: a timeout, whatever the cleanup kill makes the exit code
                return None
            data: bytes = reader.recv_bytes(cap)  # the cap is enforced here too: an over-long reply raises
        except EOFError:
            return None  # the child exited (or was killed) without replying
        except OSError as e:
            if killed.is_set() or str(e) != "bad message length":
                return None  # killed or died part-way through its reply: _why says which
            target.kill()  # longer than the cap: never read, and the child is stopped
            raise IsolationRefused("too_large", REPLY_TOO_LARGE.format(mb=self.result_mb)) from None
        return data

    def _reap(self, proc: Any, target: Any = None) -> tuple[int | None, bool]:
        """Kill the child if it is still running and wait for it; one that won't go poisons the engine.

        Returns its exit code and whether this reap had to kill it (a death the server caused, not memory).
        """
        forced = False
        proc.join(0.05)
        if proc.is_alive():
            forced = True
            (target or Target(proc, pidfd=False)).kill()
            proc.join(self.kill_wait_s)
        if proc.is_alive():
            with self._lock:
                self._unreaped.append(proc)
            return None, forced
        code: int | None = proc.exitcode
        with contextlib.suppress(Exception):
            proc.close()  # releases the sentinel now, not at garbage collection
        return code, forced


MEMORY_DEATHS = frozenset(-int(getattr(signal, n)) for n in ("SIGKILL", "SIGABRT") if hasattr(signal, n))


class Target:
    """One child to signal: through a pidfd on Linux (it can't reach a process that later reuses the PID)."""

    def __init__(self, proc: Any, *, pidfd: bool = True) -> None:
        self.proc = proc
        self.fd: int | None = None
        if pidfd and hasattr(os, "pidfd_open") and isinstance(getattr(proc, "pid", None), int):
            with contextlib.suppress(OSError):
                self.fd = os.pidfd_open(proc.pid)

    def kill(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):  # already gone
                signal.pidfd_send_signal(self.fd, signal.SIGKILL)
            return
        with contextlib.suppress(OSError, ValueError, AttributeError):  # already gone
            self.proc.kill()

    def close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None

"""Paging: a long answer comes back as pages of real rows, never as a "narrow your question" refusal.

Every tool that returns rows answers through `respond`, so stdio, the hosted HTTP server and the command line all
page the same way:

- The page budget (HDR_PAGE_CHARS, default 120,000 characters) counts both copies a client receives: the text
  block and its structured copy. A page never goes over it: pages are cut from estimated row sizes, then each page
  is measured and the cut redone when one is over. A row too long to fit on any page is refused in plain words.
- When the full answer fits, it comes back as before, plus its provenance. When it doesn't, page 1 comes back with
  rows, a header ("Page 1 of N: rows 1–k of T. The full answer has T rows."), a Next line naming the exact call for
  the next page, and the guidance to fetch every page and save the rows to one CSV file or artifact.
- Rows are in a fixed order (the compiled SQL ends in ORDER BY ALL; query_sql adds it when the query has none), and
  pages are cut from that order by size alone, so the same data always gives the same pages.
- The pin names the version of every source an answer read (snapshot id, and the hub release tag when it came from
  the hub), the number of rows in the whole answer and a hash of their keys, and is signed (HMAC-SHA256 with
  HDR_SIGNING_KEY, or a random per-process key), so a pin the server didn't write is refused and can't send it to
  another hub commit. A later page with the pin reads that same version, from the pinned hub release or lake
  snapshot if this server no longer serves it by default; if it is gone, or the rows no longer match the first
  page's, the page is refused in plain words, never mixed.
- Provenance is on every page: the same `Provenance for …` line every answer carries (tools.provenance_note:
  source URL, retrieved_at, raw-file sha256, snapshot, contract version and status, hub release, licence,
  citation), plus the answer's own notes; rows keep every column (period_status, computed and the rest).
- A public-domain source read from a hub release also gets the release file's direct download link, so a whole
  dataset can be downloaded rather than paged. A restricted source (redistribution false) never gets one. The link
  is a static release file path: it never carries a question or an argument.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import hmac
import io
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from health_data_registry.helpers import PAGING_GUIDANCE
from health_data_registry.query import QueryRefused

if TYPE_CHECKING:
    from health_data_registry.query import Engine
    from health_data_registry.registry import Dataset
    from health_data_registry.store import Manifest
    from health_data_registry.tools import Fmt, Out, Result

DEFAULT_PAGE_CHARS = 120_000
MIN_PAGE_CHARS = 2_000
HF_WEB = "https://huggingface.co/datasets/"
_PIN_PART = re.compile(r"^([a-z0-9_]{1,64}):([A-Za-z0-9._-]{1,80})(?:@([A-Za-z0-9._-]{1,80}))?$")
_MAX_PIN_CHARS = 2000
_PIN_TAIL = re.compile(r"^(0|[1-9][0-9]{0,8})~([0-9a-f]{16})~([0-9a-f]{32})$")
_PROCESS_KEY = secrets.token_bytes(
    32
)  # used when HDR_SIGNING_KEY is unset: signatures then last as long as the process

Pins = dict[str, tuple[str, str | None]]  # dataset id -> (snapshot id, hub release tag or None)


class Pin(NamedTuple):
    """What a Next line's pin holds: the versions read, and the size and row-key hash of the whole answer."""

    versions: Pins
    rows: int
    keys: str


def page_budget() -> int:
    """Characters one page may use on the wire, both copies counted (HDR_PAGE_CHARS).

    A value that isn't a whole number of at least MIN_PAGE_CHARS is refused, never read as "no limit" or as a
    budget so small that every row needs its own page.
    """
    raw = os.environ.get("HDR_PAGE_CHARS")
    if not raw:
        return DEFAULT_PAGE_CHARS
    value = int(raw) if raw.strip().isdigit() else 0
    if value < MIN_PAGE_CHARS:
        raise ValueError(
            f"HDR_PAGE_CHARS must be a whole number of at least {MIN_PAGE_CHARS:,}; got {raw!r}. Unset it to use "
            f"{DEFAULT_PAGE_CHARS:,}."
        )
    return value


def wire_size(out: Out) -> int:
    """Characters on the wire, as a client might count them: the text block plus its structured copy."""
    text = out if isinstance(out, str) else json.dumps(out, indent=2, default=str)
    return 2 * len(text)


# --------------------------------------------------------------------------------------------- the pin


def server_key(purpose: str) -> bytes:
    """The key that signs what this server hands out for `purpose` (pins, session ids).

    Derived from HDR_SIGNING_KEY (a secret, so signatures survive a restart and hold across replicas), else from a
    random per-process key. Each purpose gets its own key, so a value signed for one is never valid for another.
    """
    env = os.environ.get("HDR_SIGNING_KEY", "")
    root = env.encode() if env else _PROCESS_KEY
    return hmac.new(root, purpose.encode(), hashlib.sha256).digest()


def sign(purpose: str, body: str) -> str:
    """A 128-bit HMAC-SHA256 signature of `body` for `purpose`, as 32 hex characters."""
    return hmac.new(server_key(purpose), body.encode(), hashlib.sha256).hexdigest()[:32]


def _sign(body: str) -> str:
    return sign("pin", body)


def row_keys(rows: Sequence[Sequence[Any]]) -> str:
    """A hash of the rows' keys: every value but the decimals, whose last digits can differ between two runs of
    the same sum. Two runs over the same rows give the same hash; a lost, added or reordered row changes it."""
    h = hashlib.sha256()
    for row in rows:
        key = [None if isinstance(v, float) else v for v in row]
        h.update(json.dumps(key, default=str).encode())
        h.update(b"\n")
    return h.hexdigest()[:16]


def versions_token(used: Sequence[tuple[Dataset, Manifest]]) -> str:
    """The versions an answer read, as `<source>:<snapshot>[@<release>]` joined by commas (sorted, stable)."""
    parts = sorted(
        f"{ds.id}:{m.snapshot_id}" + (f"@{m.release_ref}" if m.release_ref else "") for ds, m in used
    )
    return ",".join(parts)


def pin_token(used: Sequence[tuple[Dataset, Manifest]], rows: Sequence[Sequence[Any]]) -> str:
    """The signed pin: `<versions>~<row count>~<row-key hash>~<signature>`."""
    body = f"{versions_token(used)}~{len(rows)}~{row_keys(rows)}"
    return f"{body}~{_sign(body)}"


BAD_PIN = (
    "The pin with this page doesn't match the one the previous page's Next line gave (it may have been cut short "
    "or changed, or the server has restarted since), so no rows are shown. Copy the Next line from the previous "
    "page exactly, or ask again without page and pin to start from page 1."
)


def parse_pin(token: str) -> Pin:
    """The pin a Next line gave, checked against its signature; anything else is refused in plain words."""
    bad = QueryRefused("bad_pin", BAD_PIN)
    if not token or len(token) > _MAX_PIN_CHARS:
        raise bad
    versions, _, tail = token.partition("~")
    found_tail = _PIN_TAIL.fullmatch(tail)
    if found_tail is None:
        raise bad
    rows, keys, sig = found_tail.groups()
    if not hmac.compare_digest(sig, _sign(f"{versions}~{rows}~{keys}")):
        raise bad
    pins: Pins = {}
    for part in versions.split(","):
        found = _PIN_PART.fullmatch(part)
        if found is None or found.group(1) in pins:
            raise bad
        pins[found.group(1)] = (found.group(2), found.group(3))
    return Pin(pins, int(rows), keys)


def data_changed(pins: Pins) -> QueryRefused:
    what = "; ".join(
        f"{i} version {s}" + (f" (hub release {r})" if r else "") for i, (s, r) in sorted(pins.items())
    )
    return QueryRefused(
        "page_data_changed",
        f"This page can't be shown: the data the first page came from ({what}) is no longer available here, and "
        "a page from newer data wouldn't fit with the pages you already have, so no rows are shown. Ask the same "
        "question again without page and pin to start from page 1 with the current data, and set aside the "
        "pages you saved from the older version.",
    )


def rows_changed(was: int, now: int) -> QueryRefused:
    change = f"it had {was:,} rows and now has {now:,}" if was != now else "its rows are no longer the same"
    return QueryRefused(
        "page_rows_changed",
        f"This page can't be shown: the answer no longer matches the first page ({change}), so a page cut from it "
        "wouldn't fit with the pages you already have, and no rows are shown. Ask the same question again without "
        "page and pin to start from page 1, and set aside the pages you saved.",
    )


# ------------------------------------------------------------------------------------- what every page says


def provenance_lines(used: Sequence[tuple[Dataset, Manifest]]) -> list[str]:
    """The provenance line (tools.provenance_note) for every source an answer read, so every page carries it."""
    from health_data_registry.tools import provenance_note

    return [provenance_note(ds, m) for ds, m in used]


def download_lines(engine: Engine, used: Sequence[tuple[Dataset, Manifest]]) -> list[str]:
    """Direct links to a whole release file, only for redistributable sources read from a hub release."""
    from health_data_registry.publish import HF_PREFIX, data_path, hf_repo_id

    if engine.hub is None or not engine.hub.url.startswith(HF_PREFIX):
        return []
    repo = hf_repo_id(engine.hub.url)
    lines = []
    for ds, m in used:
        # Restricted terms page only; a maintainer's lake copy and a live read have no public release file.
        if ds.entry.licence.redistribution and m.origin == "hub":
            path = data_path(ds.id)  # the release manifest keys its files by this path
            lines.append(
                f"Whole dataset {ds.id}: rather than paging, download the release file {HF_WEB}{repo}/resolve/"
                f"{m.release_ref}/{path} (or {HF_PREFIX}{repo}@{m.release_ref}/{path}), sha256 "
                f"{m.release_files.get(path, 'not recorded')}. It holds every row of {ds.entry.title}, not only "
                f"this answer's. Licence {ds.entry.licence.id} ({ds.entry.licence.terms_url}); cite as: "
                f"{ds.entry.citation}"
            )
    return lines


def call_text(tool: str, args: Mapping[str, Any], page: int, pin: str) -> str:
    """The exact tool call for a page: the same arguments, then page and pin."""
    shown = []
    for k, v in args.items():
        if v is None or v == "" or (isinstance(v, list | tuple) and not v) or (k == "fmt" and v == "csv"):
            continue
        shown.append(f"{k}={list(v) if isinstance(v, tuple) else v!r}")
    shown += [f"page={page}", f"pin={pin!r}"]
    return f"{tool}({', '.join(shown)})"


def header(page: int, pages: int, first: int, last: int, total: int) -> str:
    return (
        f"Page {page} of {pages}: rows {first:,}–{last:,} of {total:,}. The full answer has {total:,} rows."
    )


# ------------------------------------------------------------------------------------------- cutting pages


def _row_cost(row: Sequence[Any], fmt: Fmt) -> int:
    if fmt == "json":
        # A row in json.dumps(..., indent=2) at depth 2: its own lines, each indented 4 more, plus ",\n".
        return len(json.dumps(list(row), indent=2, default=str)) + 4 * (len(row) + 2) + 2
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(row)  # csv writes None as an empty cell, as render does
    return len(buf.getvalue())


def cut(costs: Sequence[int], room: int) -> list[tuple[int, int]]:
    """Greedy cuts: as many rows per page as fit in `room` characters, at least one, in order."""
    bounds: list[tuple[int, int]] = []
    start, used = 0, 0
    for i, c in enumerate(costs):
        if i > start and used + c > room:
            bounds.append((start, i))
            start, used = i, 0
        used += c
    bounds.append((start, len(costs)))
    return bounds


def _render_page(res: Result, lo: int, hi: int, page: int, bounds: Sequence[tuple[int, int]], tool: str,
                 args: Mapping[str, Any], token: str, extra: Sequence[str], fmt: Fmt) -> Out:  # fmt: skip
    from health_data_registry.tools import Result

    pages, total = len(bounds), len(res.rows)
    head = header(page, pages, lo + 1, hi, total)
    nxt = (
        [f"Next: {call_text(tool, args, page + 1, token)}"]
        if page < pages
        else ["This is the last page: with every page saved, the answer is complete."]
    )
    guidance = [PAGING_GUIDANCE.format(pages=pages, total=f"{total:,}")]
    notes = [*nxt, *guidance, *res.notes, *extra]
    body = Result(res.columns, res.rows[lo:hi], notes, dict(res.extra)).render(fmt)
    if isinstance(body, dict):
        info = {
            "page": page,
            "pages": pages,
            "first_row": lo + 1,
            "last_row": hi,
            "total_rows": total,
            "pin": token,
        }
        info["next"] = call_text(tool, args, page + 1, token) if page < pages else None
        return {"page": {"summary": head, **info}, **body}
    return f"# {head}\n{body}"


def plan(res: Result, fmt: Fmt, budget: int, render: Callable[[int, int, int, list[tuple[int, int]]], Out]) -> list[tuple[int, int]]:  # fmt: skip
    """Page bounds for the rows; depends only on the rows and the notes, so the same data gives the same pages.

    The cut is a heuristic: row costs are estimates (a CSV row's line, a JSON row at its indent), and a page
    without rows is measured at its widest (every number at its most digits, with a Next line and as the last
    page). So the plan is then checked: every page with more than one row is rendered and measured, and when one
    is over `budget` the cut is redone with that much less room, until none is. A row too long for any page still
    gets a page of its own, over budget; `respond` refuses that page in plain words rather than send it.
    """
    costs = [_row_cost(r, fmt) for r in res.rows]
    n = len(res.rows) + 1
    widest = max(wire_size(render(n, n, n - 1, [(0, 0)] * n)), wire_size(render(n, n, n, [(0, 0)] * n)))
    room = budget // 2 - widest // 2 - (2 if fmt == "json" else 0)
    while True:
        bounds = cut(costs, room)
        over = max(
            (
                wire_size(render(lo, hi, i + 1, bounds)) - budget
                for i, (lo, hi) in enumerate(bounds)
                if hi - lo > 1
            ),
            default=0,
        )
        if over <= 0:
            return bounds
        # Both copies count, so the text shrinks by half the overshoot, and by at least a tenth of the room each
        # pass: a steady overshoot of a few characters (a page number gaining a digit) ends in a few passes.
        room -= max(1, over // 2, room // 10)


def row_too_long(size: int, limit: int) -> str:
    """The plain refusal for a page that is one row too long to send, whatever the page budget allows."""
    return (
        f"One row of this answer is too long to send in a reply (about {size:,} characters; a page may be "
        f"{limit:,}), so nothing was sent. Ask again for fewer columns or one measure, or use describe_dataset to "
        "see which columns hold long text."
    )


# ------------------------------------------------------------------------------------------------ respond


def used_sources(engine: Engine) -> list[tuple[Dataset, Manifest]]:
    """The sources (with the versions) the last call read, in the order it first read them."""
    reg = engine.registry
    return [(reg.get(i), engine.manifests[i]) for i in engine.touched if i in engine.manifests]


def respond(
    tool: str,
    args: Mapping[str, Any],
    engine: Engine,
    body: Callable[[Engine], Result | Out],
    *,
    page: int | None,
    pin: str | None,
    fmt: Fmt,
    budget: int | None = None,
) -> Out:
    """Run one row-returning tool body and answer with the page asked for (page 1 by default)."""
    from health_data_registry.tools import Result, message, refusal

    if page is not None and page < 1:
        return message(
            "Pages are numbered from 1. Ask for page=1, or leave page out.", fmt, refused="bad_page"
        )
    given: Pin | None = None
    try:
        if pin:
            given = parse_pin(pin)
            eng = engine.at(given.versions)
            if eng is None:
                raise data_changed(given.versions)
        else:
            eng = engine
    except QueryRefused as e:
        return refusal(e, fmt)
    eng.touched.clear()
    res = body(eng)
    if not isinstance(res, Result):
        return res
    used = used_sources(eng)
    if given is not None:
        if {ds.id: (m.snapshot_id, m.release_ref) for ds, m in used} != given.versions:
            return refusal(data_changed(given.versions), fmt)
        if (len(res.rows), row_keys(res.rows)) != (given.rows, given.keys):
            return refusal(rows_changed(given.rows, len(res.rows)), fmt)
    token = pin_token(used, res.rows)
    res.notes = list(dict.fromkeys([*res.notes, *provenance_lines(used)]))
    limit = budget if budget is not None else page_budget()
    whole = res.render(fmt)
    if wire_size(whole) <= limit:  # the whole answer is one page, whatever page was asked for
        if page in (None, 1):
            return whole
        return message(
            f"This answer fits on one page, so there is no page {page}. Leave page out, or ask for page=1.",
            fmt,
            refused="no_such_page",
        )
    downloads = download_lines(eng, used)

    def render(lo: int, hi: int, n: int, bounds: list[tuple[int, int]]) -> Out:
        return _render_page(res, lo, hi, n, bounds, tool, args, token, downloads, fmt)

    if len(res.rows) <= 1:  # one row too long for a page: no cut can help, and it is never sent over budget
        return message(row_too_long(wire_size(whole), limit), fmt, refused="too_large")
    bounds = plan(res, fmt, limit, render)
    n = page or 1
    if n > len(bounds):
        last = len(bounds)
        return message(
            f"This answer fits on one page, so there is no page {n}. Leave page out, or ask for page=1."
            if last == 1
            else f"There is no page {n}: this answer ends at page {last}. Ask for a page from 1 to {last}, or leave "
            "page out for page 1.",
            fmt,
            refused="no_such_page",
        )
    # Even a plan of one page is rendered and measured: `whole` is already over the limit.
    lo, hi = bounds[n - 1]
    out = render(lo, hi, n, bounds)
    if wire_size(out) > limit:  # only a page of one row can be over (plan re-cuts the rest): never sent
        return message(row_too_long(wire_size(out), limit), fmt, refused="too_large")
    return out


def paged(tool: str) -> Callable[[Callable[..., Result | Out]], Callable[..., Out]]:
    """Turn a tool body `fn(registry, engine, *, ..., fmt)` that returns a Result into a paged tool.

    The paged tool takes the same arguments plus `page` (1-based) and `pin` (from the previous page's Next line).
    """

    def deco(fn: Callable[..., Result | Out]) -> Callable[..., Out]:
        @functools.wraps(fn)
        def wrapper(
            registry: Any, engine: Engine, *, page: int | None = None, pin: str | None = None, fmt: Fmt = "csv",
            **kw: Any,
        ) -> Out:  # fmt: skip
            args = {**kw, "fmt": fmt}
            return respond(
                tool, args, engine, lambda eng: fn(registry, eng, fmt=fmt, **kw), page=page, pin=pin, fmt=fmt
            )

        return wrapper

    return deco

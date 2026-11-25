"""Paging: a long answer comes back as pages of real rows with provenance on each, never as a refusal."""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from health_data_registry import analysis, cli, geo, paging, tools
from health_data_registry.hosted import CallGuard, HostedConfig, Metrics
from health_data_registry.hub import Hub
from health_data_registry.publish import export_public
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry, load_registry
from health_data_registry.sql_guard import guard_sql
from health_data_registry.store import build_snapshot, latest_snapshot, write_ndjson
from tests.conftest import raw_fetch, write_registry
from tests.test_publish import write_hub_registry

STATES = [p.name for p in geo.PLACES if p.level == "state"]
RUN, RUN2 = "2026-11-24", "2026-11-25"
TOTAL = len(STATES) * 3 * 2  # legionellosis has a count series and a rate series
HEADER = re.compile(
    r"^Page (\d+) of (\d+): rows ([\d,]+)–([\d,]+) of ([\d,]+)\. The full answer has \5 rows\.$"
)


def big_records(years: range = range(2022, 2025), bump: int = 0) -> list[dict[str, str]]:
    return [
        {"states": s.upper(), "year": str(y), "label": "Legionellosis", "m1": str(100 + i + y + bump),
         "m2": f"{(i % 9) + 0.5}"}
        for i, s in enumerate(STATES)
        for y in years
    ]  # fmt: skip


def snapshot(lake: Path, reg: Registry, tmp: Path, records: list[dict[str, str]], day: int = 24) -> str:
    lake.mkdir(parents=True, exist_ok=True)
    f = tmp / f"records-{day}-{len(records)}.ndjson"
    write_ndjson(f, records)
    when = datetime(2026, 11, day, 12, 0, tzinfo=UTC)
    m = build_snapshot(lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json",
                                                              records, when)], [f])  # fmt: skip
    return m.snapshot_id


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return load_registry(write_registry(tmp_path / "reg"))


@pytest.fixture
def lake(tmp_path: Path, registry: Registry) -> Path:
    lake = tmp_path / "lake"
    snapshot(lake, registry, tmp_path, big_records())
    return lake


@pytest.fixture
def eng(registry: Registry, lake: Path) -> Engine:
    return Engine(registry=registry, lake=lake, hub=None)


def series(reg: Registry, e: Engine, **kw: Any) -> Any:
    return tools.get_series(reg, e, condition="legionellosis", **kw)


def csv_rows(text: str) -> tuple[list[str], list[list[str]]]:
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    table = list(csv.reader(io.StringIO("\n".join(lines))))
    return table[0], [r for r in table[1:] if r]


def signed(versions: str, rows: int = 0, keys: str = "0" * 16) -> str:
    """A pin this server would accept: the versions, a row count and row-key hash, and their signature."""
    body = f"{versions}~{rows}~{keys}"
    return f"{body}~{paging.sign('pin', body)}"


def next_call(out: Any) -> tuple[int, str] | None:
    """(page, pin) from a page's Next line, or None on the last page."""
    text = out if isinstance(out, str) else "\n".join(out["notes"])
    found = re.search(r"Next: get_series\(.*page=(\d+), pin='([^']+)'\)", text)
    return (int(found.group(1)), found.group(2)) if found else None


def walk(reg: Registry, e: Engine, fmt: str, **kw: Any) -> list[Any]:
    pages = [series(reg, e, fmt=fmt, **kw)]
    while (nxt := next_call(pages[-1])) is not None:
        assert nxt[0] == len(pages) + 1
        pages.append(series(reg, e, fmt=fmt, page=nxt[0], pin=nxt[1], **kw))
    return pages


# ------------------------------------------------------------------------------------------------ page one


def test_over_budget_gives_page_one_with_rows_a_header_and_the_next_call(
    registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = series(registry, eng)
    assert isinstance(whole, str) and "Page 1 of" not in whole  # fits the default budget: as before
    monkeypatch.setenv("HDR_PAGE_CHARS", "8000")
    out = series(registry, eng, locations=[])
    assert isinstance(out, str)
    first = out.splitlines()[0]
    found = HEADER.match(first.removeprefix("# "))
    assert found, first
    page, pages, lo, hi, total = (int(g.replace(",", "")) for g in found.groups())
    assert (page, lo) == (1, 1) and pages > 1 and total == TOTAL
    cols, rows = csv_rows(out)
    assert cols[:3] == ["location", "year", "value"] and len(rows) == hi
    m = latest_snapshot(eng.lake, "toy_cases")
    assert m is not None
    nxt = next_call(out)
    assert nxt is not None and nxt[0] == 2
    pin = nxt[1]
    assert pin.startswith(f"toy_cases:{m[1].snapshot_id}~{TOTAL}~")  # versions, then the whole answer's size
    assert paging.parse_pin(pin).rows == TOTAL
    assert f"# Next: get_series(condition='legionellosis', page=2, pin='{pin}')" in out
    assert "save all the rows to one CSV file or artifact rather than pasting them into the chat" in out
    assert paging.wire_size(out) <= 8000
    assert "too long" not in out and "Narrow" not in out


def test_json_pages_say_where_they_are(registry: Registry, eng: Engine) -> None:
    out = paging.respond(
        "get_series", {"condition": "legionellosis", "fmt": "json"}, eng,
        lambda e: tools.get_series.__wrapped__(registry, e, condition="legionellosis", fmt="json"),
        page=2, pin=None, fmt="json", budget=8000,
    )  # fmt: skip
    assert isinstance(out, dict)
    info = out["page"]
    assert info["page"] == 2 and info["first_row"] > 1
    assert info["last_row"] - info["first_row"] + 1 == len(out["rows"])
    assert info["total_rows"] == TOTAL and info["pin"].startswith("toy_cases:")
    assert info["summary"].startswith("Page 2 of ")
    assert info["next"] is None or "page=3" in info["next"]
    assert paging.wire_size(out) <= 8000


# --------------------------------------------------------------------------------- pages rebuild the answer


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(budget=st.integers(min_value=12_000, max_value=60_000), fmt=st.sampled_from(["csv", "json"]))
def test_walking_every_page_rebuilds_exactly_the_full_answer(
    registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch, budget: int, fmt: str
) -> None:
    monkeypatch.setenv("HDR_PAGE_CHARS", str(10**9))
    full = series(registry, eng, fmt=fmt)
    monkeypatch.setenv("HDR_PAGE_CHARS", str(budget))
    pages = walk(registry, eng, fmt)
    totals = set()
    for p in pages:
        assert paging.wire_size(p) <= budget
    if fmt == "json":
        assert [r for p in pages for r in p["rows"]] == full["rows"]
        assert all(p["columns"] == full["columns"] for p in pages)
        totals = {p["page"]["total_rows"] for p in pages} if len(pages) > 1 else {len(full["rows"])}
    else:
        rows = [r for p in pages for r in csv_rows(p)[1]]
        assert rows == csv_rows(full)[1]
        totals = {len(rows)}
    assert totals == {TOTAL}


def test_pages_are_the_same_every_time(
    registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HDR_PAGE_CHARS", "9000")
    a = walk(registry, eng, "csv")
    b = walk(registry, Engine(registry=registry, lake=eng.lake, hub=None), "csv")
    assert a == b and len(a) > 2


def test_provenance_is_on_every_page(
    registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HDR_PAGE_CHARS", "9000")
    found = latest_snapshot(eng.lake, "toy_cases")
    assert found is not None
    m = found[1]
    pages = walk(registry, eng, "csv")
    assert len(pages) > 2
    line = (
        f"# Provenance for toy_cases: https://data.cdc.gov/d/toy0-toy0; retrieved_at {m.retrieved_at[:19]}; raw file "
        f"sha256 {m.raw_sha256[0]}; snapshot {m.snapshot_id}; contract version 0.1.0 (contract_status: "
        f"{m.catalog_status}); licence public-domain-us-federal (https://www.cdc.gov/other/agencymaterials.html); "
        "cite as: CDC. Toy table. https://data.cdc.gov/d/toy0-toy0"
    )
    assert line == "# " + tools.provenance_note(registry.get("toy_cases"), m)
    for p in pages:
        assert line in p.splitlines()
        assert "Whole dataset" not in p  # a maintainer's lake copy has no public release file to offer
    assert line in series(registry, eng, locations=["Ohio"])  # a one-page answer carries it too
    assert "This is the last page" in pages[-1] and "Next:" not in pages[-1]


def test_every_paged_tool_carries_provenance(registry: Registry, eng: Engine) -> None:
    calls = [
        tools.get_aligned_with_gbd(registry, eng, condition="legionellosis"),
        tools.get_provenance(registry, eng, source_id="toy_cases"),
        tools.query_sql(eng, "SELECT reporting_area, year FROM toy_cases"),
    ]
    for out in calls:
        assert "# Provenance for toy_cases: https://data.cdc.gov/d/toy0-toy0;" in out


# ------------------------------------------------------------------------------------------ plain refusals


def test_bad_pages_and_pins_are_refused_in_plain_words(registry: Registry, eng: Engine) -> None:
    assert series(registry, eng, page=0) == "Pages are numbered from 1. Ask for page=1, or leave page out."
    out = series(registry, eng, locations=["Ohio"], page=5, fmt="json")
    assert out["refused"] == "no_such_page" and out["message"] == (
        "This answer fits on one page, so there is no page 5. Leave page out, or ask for page=1."
    )
    for pin in ("nonsense", "toy_cases:../../etc", "a:b,a:b", "x" * 3000, "toy_cases:s1@r1@r2"):
        bad = series(registry, eng, page=2, pin=pin, fmt="json")
        assert bad["refused"] == "bad_pin" and "Copy the Next line" in bad["message"], pin


def test_a_pinned_version_that_is_gone_gives_a_plain_refusal(
    tmp_path: Path, registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HDR_PAGE_CHARS", "9000")
    first = series(registry, eng)
    nxt = next_call(first)
    assert nxt is not None
    page2 = series(registry, eng, page=2, pin=nxt[1])
    # A newer snapshot lands; the pinned one is still in the lake, so page 2 still comes from it.
    snapshot(eng.lake, registry, tmp_path, big_records(bump=7), day=25)
    fresh = Engine(registry=registry, lake=eng.lake, hub=None)
    assert fresh.manifest("toy_cases").snapshot_id != nxt[1].split("~")[0].split(":")[1]
    assert series(registry, fresh, page=2, pin=nxt[1]) == page2
    assert next_call(series(registry, fresh)) != nxt  # page 1 now pins the new data
    # The pinned snapshot is removed: refused in plain words, never a page from other data.
    old = eng.lake / "datasets" / "toy_cases" / "run_date=2026-11-24"
    shutil.rmtree(old)
    gone = series(
        registry, Engine(registry=registry, lake=eng.lake, hub=None), page=2, pin=nxt[1], fmt="json"
    )
    assert gone["refused"] == "page_data_changed"
    assert "no longer available here" in gone["message"] and "start from page 1" in gone["message"]
    assert "Traceback" not in gone["message"] and "Error" not in gone["message"]
    unknown = series(
        registry, eng, page=2, pin=signed("other_source:20261124T120000Z-abcdefabcdef"), fmt="json"
    )
    assert unknown["refused"] == "page_data_changed"


# ------------------------------------------------------------------------- hub releases and download links


@pytest.fixture
def releases(tmp_path: Path) -> tuple[Registry, dict[str, Path]]:
    reg = load_registry(write_hub_registry(tmp_path / "hreg"))
    out: dict[str, Path] = {}
    for run, bump, day in ((RUN, 0, 24), (RUN2, 5, 25)):
        lake = tmp_path / f"maint-{run}"
        snapshot(lake, reg, tmp_path, big_records(bump=bump), day=day)
        out[f"release-{run}"] = tmp_path / f"hub-{run}"
        export_public(reg, lake, str(out[f"release-{run}"]), run_date=run)
    return reg, out


@pytest.fixture
def fake_hf(monkeypatch: pytest.MonkeyPatch, releases: tuple[Registry, dict[str, Path]]) -> set[str]:
    """hf_hub_download served from local release directories; drop a tag from the returned set to 'delete' it."""
    import huggingface_hub

    live = set(releases[1])

    def download(repo: str, filename: str, **kw: Any) -> str:
        if kw["revision"] not in live:
            raise OSError("404")
        target = Path(kw["local_dir"]) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(releases[1][kw["revision"]] / filename, target)
        return str(target)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    return live


def hub_engine(reg: Registry, tmp_path: Path, tag: str, **kw: Any) -> Engine:
    h = Hub(url=f"hf://datasets/org/hub@{tag}", cache=tmp_path / "hubcache")
    return Engine(registry=reg, lake=tmp_path / "no-lake", hub=h, cache=tmp_path / "hubcache", **kw)


def test_public_domain_hub_source_gets_a_static_download_link(
    tmp_path: Path, releases: tuple[Registry, dict[str, Path]], fake_hf: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    reg = releases[0]
    monkeypatch.setenv("HDR_PAGE_CHARS", "9000")
    e = hub_engine(reg, tmp_path, f"release-{RUN}")
    out = tools.get_series(
        reg, e, condition="legionellosis", locations=["Ohio", "Texas"] * 0, year_start=2022
    )
    assert out.startswith("# Page 1 of ")
    link = [ln for ln in out.splitlines() if ln.startswith("# Whole dataset toy_cases:")]
    assert len(link) == 1
    urls = re.findall(r"(?:https|hf)://\S+?(?=[ ,)])", link[0])
    assert (
        f"https://huggingface.co/datasets/org/hub/resolve/release-{RUN}/toy_cases/data/toy_cases.parquet"
        in urls
    )
    assert f"hf://datasets/org/hub@release-{RUN}/toy_cases/data/toy_cases.parquet" in urls
    static = re.compile(
        r"^(https://huggingface\.co/datasets/org/hub/resolve/|hf://datasets/org/hub@)release-\d{4}-\d{2}-\d{2}"
        r"/toy_cases/data/toy_cases\.parquet$"
    )
    hub_urls = [u for u in urls if "huggingface" in u or u.startswith("hf://")]
    assert hub_urls and all(
        static.fullmatch(u) for u in hub_urls
    )  # no question, no argument, no query string
    assert "legionellosis" not in " ".join(hub_urls).lower() and "?" not in " ".join(hub_urls)
    assert "Licence public-domain-us-federal" in link[0] and "cite as: CDC. Toy table." in link[0]
    sha = e.manifest("toy_cases").release_files["toy_cases/data/toy_cases.parquet"]
    assert len(sha) == 64 and f"), sha256 {sha}. It holds every row of" in link[0]


def test_restricted_source_never_gets_a_download_link(
    tmp_path: Path, releases: tuple[Registry, dict[str, Path]], fake_hf: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    reg = releases[0]
    monkeypatch.setenv("HDR_PAGE_CHARS", "9000")
    records = big_records(years=range(2000, 2025))

    def get(url: str) -> Any:
        return raw_fetch(url, records)

    e = hub_engine(reg, tmp_path, f"release-{RUN}", fetch_at_run_time=True, getter=get)
    out = tools.query_sql(e, "SELECT reporting_area, year, cases FROM toy_listed")
    assert out.startswith("# Page 1 of ")
    assert "Whole dataset" not in out and "resolve/" not in out and "hf://" not in out
    assert "# Provenance for toy_listed:" in out and "CC-BY-NC-SA-3.0-IGO" in out
    found = re.search(r"Next: query_sql\(.*page=2, pin='([^']+)'\)", out)
    assert found and found.group(1).startswith("toy_listed:live-")
    page2 = tools.query_sql(
        e, "SELECT reporting_area, year, cases FROM toy_listed", page=2, pin=found.group(1)
    )
    assert page2.startswith("# Page 2 of ") and "Whole dataset" not in page2
    # A restricted copy is never kept, so once the in-memory read is gone its pages can't be continued.
    gone = tools.query_sql(
        hub_engine(reg, tmp_path, f"release-{RUN}", fetch_at_run_time=False),
        "SELECT reporting_area, year, cases FROM toy_listed", page=2, pin=found.group(1), fmt="json",
    )  # fmt: skip
    assert gone["refused"] == "page_data_changed"


def test_a_new_hub_release_keeps_old_pages_on_the_pinned_release(
    tmp_path: Path, releases: tuple[Registry, dict[str, Path]], fake_hf: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    reg = releases[0]
    monkeypatch.setenv("HDR_PAGE_CHARS", "9000")
    old = hub_engine(reg, tmp_path, f"release-{RUN}")
    first = tools.get_series(reg, old, condition="legionellosis")
    nxt = next_call(first)
    assert nxt is not None and f"@release-{RUN}~" in nxt[1]
    page2 = tools.get_series(reg, old, condition="legionellosis", page=2, pin=nxt[1])
    new = hub_engine(reg, tmp_path, f"release-{RUN2}")  # a restart picked up the newer release
    assert tools.get_series(reg, new, condition="legionellosis", page=2, pin=nxt[1]) == page2
    assert tools.get_series(reg, new, condition="legionellosis", page=2, pin=nxt[1]) == page2  # kept engine
    fake_hf.discard(f"release-{RUN}")
    shutil.rmtree(tmp_path / "hubcache" / "hub" / "org--hub" / f"release-{RUN}")
    gone = tools.get_series(
        reg, hub_engine(reg, tmp_path, f"release-{RUN2}"), condition="legionellosis", page=2, pin=nxt[1],
        fmt="json",
    )  # fmt: skip
    assert gone["refused"] == "page_data_changed" and f"hub release release-{RUN}" in gone["message"]


# ------------------------------------------------------------------------------------------- HTTP and CLI


def test_http_guard_keeps_every_page_under_the_cap(
    tmp_path: Path, registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert HostedConfig(cache=tmp_path).max_result_chars > paging.DEFAULT_PAGE_CHARS
    cap = 9_000
    monkeypatch.setenv("HDR_PAGE_CHARS", str(cap - 1_000))
    cfg = HostedConfig(hub_url="unused", cache=tmp_path / "c", max_result_chars=cap)
    metrics = Metrics()
    guard = CallGuard(cfg, metrics, eng)
    page, pin, seen = 1, None, 0
    while True:
        out = guard("get_series", lambda p=page, q=pin: series(registry, eng, page=p, pin=q), "csv")
        assert paging.wire_size(out) <= cap and isinstance(out, str) and out.startswith(f"# Page {page} of ")
        seen += len(csv_rows(out)[1])
        if (nxt := next_call(out)) is None:
            break
        page, pin = nxt
    assert seen == TOTAL and page > 2
    assert not any(k[1] == "too_large" for k in metrics.counts)


def test_cli_pages_like_the_tool(
    registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HDR_PAGE_CHARS", "9000")
    monkeypatch.setenv("HDR_LAKE", str(eng.lake))
    monkeypatch.setattr(cli, "load_registry", lambda: registry)
    nxt = next_call(series(registry, eng))
    assert nxt is not None
    assert cli.main(["get-series", "legionellosis", "--page", "2", "--pin", nxt[1]]) == 0
    assert capsys.readouterr().out == series(registry, eng, page=2, pin=nxt[1])
    assert cli.main(["query-sql", "SELECT reporting_area FROM toy_cases", "--page", "2", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (
        out["page"]["page"] == 2
        and out["page"]["next"] is None
        or out["page"]["next"].startswith("query_sql(")
    )
    assert cli.main(["get-provenance", "toy_cases"]) == 0
    assert "# Provenance for toy_cases:" in capsys.readouterr().out
    assert cli.main(["get-aligned-with-gbd", "legionellosis", "--location", "Ohio"]) == 0
    assert "Ohio" in capsys.readouterr().out
    assert cli.main(["get-rate", "legionellosis", "--year", "2023", "--page", "0"]) == 0
    assert "Pages are numbered from 1" in capsys.readouterr().out


def test_analysis_tools_page_too(registry: Registry, eng: Engine) -> None:
    out = analysis.get_rate(registry, eng, condition="legionellosis", year=2023, page=3, fmt="json")
    assert out.get("refused") in ("no_such_page", "no_denominator")  # a refusal passes through unpaged


# ----------------------------------------------------------------------------------------- the pieces


def test_query_sql_rows_come_in_a_fixed_order() -> None:
    cols = {"t": frozenset({"a", "b"})}
    assert (
        guard_sql("SELECT a FROM t", allowed_tables=cols, ordered=True).sql
        == "SELECT a FROM t ORDER BY ALL LIMIT 5000"
    )
    assert guard_sql("SELECT a FROM t", allowed_tables=cols).sql == "SELECT a FROM t LIMIT 5000"
    own = guard_sql("SELECT a FROM t ORDER BY b DESC", allowed_tables=cols, ordered=True).sql
    assert own == "SELECT a FROM t ORDER BY b DESC, 1 LIMIT 5000"  # its own order, then every output column
    two = guard_sql("SELECT a, b FROM t ORDER BY b", allowed_tables=cols, ordered=True).sql
    assert two == "SELECT a, b FROM t ORDER BY b, 1, 2 LIMIT 5000"
    star = guard_sql("SELECT * FROM t ORDER BY b", allowed_tables=cols, ordered=True).sql
    assert star == "SELECT * FROM t ORDER BY b, COLUMNS(*) LIMIT 5000"
    total = guard_sql("SELECT a FROM t ORDER BY ALL", allowed_tables=cols, ordered=True).sql
    assert total == "SELECT a FROM t ORDER BY ALL LIMIT 5000"
    union = "SELECT * FROM t UNION ALL SELECT * FROM t ORDER BY a"
    assert guard_sql(union, allowed_tables=cols, ordered=True).sql == union + " LIMIT 5000"
    assert (
        guard_sql("SELECT a FROM t ORDER BY b", allowed_tables=cols).sql
        == "SELECT a FROM t ORDER BY b LIMIT 5000"
    )


def test_cut_and_call_text() -> None:
    assert paging.cut([3, 3, 3, 3], 6) == [(0, 2), (2, 4)]
    assert paging.cut([3, 3, 3, 3], 5) == [(0, 1), (1, 2), (2, 3), (3, 4)]
    assert paging.cut([10, 1], 5) == [(0, 1), (1, 2)]  # one row over the room still gets its own page
    assert paging.cut([2, 2, 2], 6) == [(0, 3)]
    assert paging.cut([], 6) == [(0, 0)]
    text = paging.call_text(
        "get_series", {"condition": "x", "locations": ("A",), "year_start": None, "measure": "", "sex": [],
                       "fmt": "csv"}, 3, "p:1",
    )  # fmt: skip
    assert text == "get_series(condition='x', locations=['A'], page=3, pin='p:1')"
    assert paging.call_text("q", {"fmt": "json"}, 2, "p:1") == "q(fmt='json', page=2, pin='p:1')"
    assert paging.header(2, 3, 1001, 2000, 2500) == (
        "Page 2 of 3: rows 1,001–2,000 of 2,500. The full answer has 2,500 rows."
    )
    assert paging.parse_pin(signed("a:s1@release-2026-11-24,b:s2", 12, "ab" * 8)) == (
        {"a": ("s1", "release-2026-11-24"), "b": ("s2", None)},
        12,
        "ab" * 8,
    )
    assert paging.wire_size("abc") == 6 and paging.wire_size({"a": 1}) == 2 * len('{\n  "a": 1\n}')


def test_page_budget_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HDR_PAGE_CHARS", raising=False)
    assert paging.page_budget() == 120_000
    monkeypatch.setenv("HDR_PAGE_CHARS", "5000")
    assert paging.page_budget() == 5000


# ------------------------------------------------------------------------------------ exact sizes and words


def test_row_costs_are_exact() -> None:
    rows: list[list[Any]] = [["Ohio", 2023, None, 'a"b,c', 1.5, "x\ny"], ["é", 1, 2, 3, 4, 5], [None] * 6]
    for fmt in ("csv", "json"):
        for k in range(3):
            before = paging.wire_size(tools.Result(list("abcdef"), rows[:k], ["n"]).render(fmt)) // 2
            after = paging.wire_size(tools.Result(list("abcdef"), rows[: k + 1], ["n"]).render(fmt)) // 2
            first = 2 if fmt == "json" and k == 0 else 0
            assert after - before == paging._row_cost(rows[k], fmt) + first, (fmt, k)
    assert paging._row_cost([None, "a"], "csv") == len(",a\n")


def _respond(registry: Registry, eng: Engine, fmt: str, page: int, pin: str | None, budget: int) -> Any:
    return paging.respond(
        "get_series", {"condition": "legionellosis", "fmt": fmt}, eng,
        lambda e: tools.get_series.__wrapped__(registry, e, condition="legionellosis", fmt=fmt),
        page=page, pin=pin, fmt=fmt, budget=budget,
    )  # fmt: skip


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_pages_are_full_up_to_the_budget(registry: Registry, eng: Engine, fmt: str) -> None:
    """Every page but the last is as full as the budget allows: the next row would not have fitted."""
    budget = 20_000
    page, pin, pages = 1, None, []
    while True:
        out = _respond(registry, eng, fmt, page, pin, budget)
        pages.append(out)
        if (nxt := next_call(out)) is None:
            break
        page, pin = nxt
    full = tools.get_series.__wrapped__(registry, eng, condition="legionellosis", fmt="json").rows
    assert len(pages) > 2
    at = 0
    for p in pages:
        size = paging.wire_size(p)
        assert size <= budget
        at += len(p["rows"]) if fmt == "json" else len(csv_rows(p)[1])
        if p is not pages[-1]:
            assert size + 2 * paging._row_cost(full[at], fmt) > budget - 60  # slack: header digits only


def test_a_pin_for_other_sources_is_refused(
    tmp_path: Path, releases: tuple[Registry, dict[str, Path]], fake_hf: set[str]
) -> None:
    reg = releases[0]
    rows = big_records()
    e = hub_engine(
        reg, tmp_path, f"release-{RUN}", fetch_at_run_time=True, getter=lambda u: raw_fetch(u, rows)
    )
    first = tools.query_sql(e, "SELECT year FROM toy_cases", fmt="json")
    pin = paging.pin_token([(reg.get("toy_cases"), e.manifest("toy_cases"))], first["rows"])
    out = tools.query_sql(e, "SELECT year FROM toy_listed", page=1, pin=pin, fmt="json")
    assert out["refused"] == "page_data_changed"
    ok = tools.query_sql(e, "SELECT year FROM toy_cases", page=1, pin=pin, fmt="json")
    assert ok["rows"]
    # The same versions but other rows (a filter that keeps fewer): refused, never a page cut from a new row set.
    fewer = tools.query_sql(e, "SELECT year FROM toy_cases WHERE year > 2022", page=1, pin=pin, fmt="json")
    assert fewer["refused"] == "page_rows_changed" and "start from page 1" in fewer["message"]


def test_one_page_answers_are_unchanged_by_page_one(registry: Registry, eng: Engine) -> None:
    one = series(registry, eng, locations=["Ohio"])
    assert series(registry, eng, locations=["Ohio"], page=1) == one
    bad = series(registry, eng, page=-1, fmt="json")
    assert bad == {
        "message": "Pages are numbered from 1. Ask for page=1, or leave page out.",
        "refused": "bad_page",
        "next_step": "Ask for page=1, or leave page out.",
    }


def test_provenance_lines_name_every_part(registry: Registry, eng: Engine) -> None:
    """Pages carry the same provenance line as every other answer (tools.provenance_note), one per source."""
    import dataclasses

    ds = registry.get("toy_cases")
    m = eng.manifest("toy_cases")
    many = dataclasses.replace(m, source_urls=["https://a/1", "https://a/2", "https://a/3"],
                               raw_sha256=["s1", "s2", "s3"], release_ref="release-2026-11-24",
                               release_id="r1")  # fmt: skip
    (line,) = paging.provenance_lines([(ds, many)])
    assert line == tools.provenance_note(ds, many)
    assert line == (
        f"Provenance for toy_cases: https://data.cdc.gov/d/toy0-toy0; retrieved_at {m.retrieved_at[:19]}; raw file "
        f"sha256 s1, s2 and 1 more (get_provenance lists every file); snapshot {m.snapshot_id}; contract version "
        f"0.1.0 (contract_status: {m.catalog_status}); hub release release-2026-11-24 (r1); licence "
        "public-domain-us-federal (https://www.cdc.gov/other/agencymaterials.html); cite as: CDC. Toy table. "
        "https://data.cdc.gov/d/toy0-toy0"
    )
    other = registry.get("toy_listed") if "toy_listed" in registry.datasets else ds
    both = paging.provenance_lines([(ds, m), (other, m)])
    assert len(both) == 2 and both[0] == tools.provenance_note(ds, m)
    assert paging.provenance_lines([]) == []


def test_data_changed_and_pin_words() -> None:
    e = paging.data_changed({"b": ("s2", None), "a": ("s1", "release-2026-11-24")})
    assert e.code == "page_data_changed"
    assert str(e) == (
        "This page can't be shown: the data the first page came from (a version s1 (hub release "
        "release-2026-11-24); b version s2) is no longer available here, and a page from newer data wouldn't fit "
        "with the pages you already have, so no rows are shown. Ask the same question again without page and pin "
        "to start from page 1 with the current data, and set aside the pages you saved from the older version."
    )
    with pytest.raises(QueryRefused) as bad:
        paging.parse_pin("")
    assert bad.value.code == "bad_pin" and str(bad.value) == (
        "The pin with this page doesn't match the one the previous page's Next line gave (it may have been cut "
        "short or changed, or the server has restarted since), so no rows are shown. Copy the Next line from the "
        "previous page exactly, or ask again without page and pin to start from page 1."
    )
    tail = len(signed("")) - 0  # "~0~<16>~<32>"
    parts = [f"d{i:03d}:" + "s" * 74 for i in range(24)]
    exactly = signed(",".join([*parts, "d999:" + "s" * (75 - tail)]))
    assert len(exactly) == 2000 and len(paging.parse_pin(exactly).versions) == 25
    with pytest.raises(QueryRefused):
        paging.parse_pin(signed(",".join([*parts, "d999:" + "s" * (76 - tail)])))
    good = signed("a:s1@release-2026-11-24", 5, "cd" * 8)
    for forged in (
        good.replace("release-2026-11-24", "release-2026-11-25"),  # another release, same signature
        good.replace("~5~", "~6~"),  # another row count
        good[:-1] + ("0" if good[-1] != "0" else "1"),  # a changed signature
        good.rsplit("~", 1)[0],  # no signature
        "a:s1@release-2026-11-24",  # the old, unsigned form
        signed("a:s1", 1, "zz" * 8),  # a malformed hash
        signed("a:s1", -1),
        signed("a:s1,a:s2"),
    ):
        with pytest.raises(QueryRefused):
            paging.parse_pin(forged)
    assert paging.parse_pin(signed("a:s1", 0)).rows == 0


def test_token_call_and_size_details() -> None:
    from datetime import date

    from health_data_registry.store import Manifest

    def man(sid: str, ref: str | None) -> Manifest:
        return Manifest(dataset="x", snapshot_id=sid, contract_version="1", semantic_version="1",
                        catalog_status="draft", retrieved_at="t", source_urls=[], raw_sha256=[], row_count=0,
                        licence="l", redistribution=True, partial=False, release_ref=ref)  # fmt: skip

    class D:
        def __init__(self, i: str) -> None:
            self.id = i

    used: Any = [(D("b"), man("s2", "release-2026-11-24")), (D("a"), man("s1", None))]
    assert paging.versions_token(used) == "a:s1,b:s2@release-2026-11-24"
    rows = [["Ohio", 2020, 1.5], ["Texas", 2021, None]]
    token = paging.pin_token(used, rows)
    assert token.startswith(f"a:s1,b:s2@release-2026-11-24~2~{paging.row_keys(rows)}~")
    assert paging.parse_pin(token) == (
        {"a": ("s1", None), "b": ("s2", "release-2026-11-24")},
        2,
        paging.row_keys(rows),
    )
    # Decimals don't count (their last digits can differ between runs); every other value does, in order.
    assert paging.row_keys([["Ohio", 2020, 1.5000000001]]) == paging.row_keys([["Ohio", 2020, 1.5]])
    assert paging.row_keys([["Ohio", 2021, 1.5]]) != paging.row_keys([["Ohio", 2020, 1.5]])
    assert paging.row_keys(rows) != paging.row_keys(rows[::-1])
    assert len(paging.row_keys([])) == 16
    assert paging.call_text("t", {"a": None, "b": 1}, 2, "p") == "t(b=1, page=2, pin='p')"
    assert paging.wire_size({"d": date(2026, 1, 2)}) == 2 * len('{\n  "d": "2026-01-02"\n}')


def test_json_last_page_says_so(registry: Registry, eng: Engine) -> None:
    first = _respond(registry, eng, "json", 1, None, 30_000)
    n, pin = first["page"]["pages"], first["page"]["pin"]
    assert first["page"]["next"] == f"get_series(condition='legionellosis', fmt='json', page=2, pin='{pin}')"
    assert f"This answer is long, so it comes in {n} pages ({TOTAL:,} rows in all)" in first["notes"][1]
    last = _respond(registry, eng, "json", n, None, 30_000)
    assert last["page"]["next"] is None and last["page"]["last_row"] == TOTAL and last["page"]["pages"] == n
    assert last["notes"][0] == "This is the last page: with every page saved, the answer is complete."
    assert _respond(registry, eng, "json", n - 1, None, 30_000)["page"]["next"].endswith(
        f"page={n}, pin='{pin}')"
    )


def test_download_lines_only_for_public_sources_read_from_a_hub_release(
    registry: Registry, eng: Engine
) -> None:
    import dataclasses
    from types import SimpleNamespace

    ds = registry.get("toy_cases")
    m = dataclasses.replace(eng.manifest("toy_cases"), release_ref="release-2026-11-24")
    remote: Any = SimpleNamespace(hub=SimpleNamespace(url="hf://datasets/org/hub@release-2026-11-24"))
    hub_copy = dataclasses.replace(m, origin="hub")
    (line,) = paging.download_lines(remote, [(ds, hub_copy)])
    assert (
        "sha256 not recorded." in line
    )  # a release that doesn't list the file says so, never a made-up hash
    assert paging.download_lines(remote, [(ds, m)]) == []  # a maintainer's lake copy has no public file
    no_sharing = SimpleNamespace(redistribution=False)
    restricted: Any = SimpleNamespace(id="toy_listed", entry=SimpleNamespace(licence=no_sharing))
    assert paging.download_lines(remote, [(restricted, hub_copy)]) == []
    assert paging.download_lines(SimpleNamespace(hub=None), [(ds, hub_copy)]) == []  # type: ignore[arg-type]
    local: Any = SimpleNamespace(hub=SimpleNamespace(url="/some/dir"))
    assert paging.download_lines(local, [(ds, hub_copy)]) == []


def test_no_such_page_counts_pages_in_words(registry: Registry, eng: Engine) -> None:
    two = _respond(registry, eng, "json", 1, None, 60_000)
    n = two["page"]["pages"]
    beyond = _respond(registry, eng, "json", n + 1, None, 60_000)
    assert beyond["message"] == (
        f"There is no page {n + 1}: this answer ends at page {n}. Ask for a page from 1 to {n}, or leave page out "
        "for page 1."
    )
    assert _respond(registry, eng, "json", 3, None, 10**9)["message"].startswith(
        "This answer fits on one page, so there is no page 3."
    )


def test_a_two_page_answer_says_pages(registry: Registry, eng: Engine) -> None:
    whole = paging.wire_size(_respond(registry, eng, "json", 1, None, 10**9))
    two = next(
        b
        for b in range(whole // 2, whole, 2_000)
        if _respond(registry, eng, "json", 1, None, b)["page"]["pages"] == 2
    )
    assert _respond(registry, eng, "json", 3, None, two)["message"].startswith(
        "There is no page 3: this answer ends at page 2."
    )


# ------------------------------------------------------------------------------------- engines on old versions


def test_engines_on_pinned_lake_snapshots_are_kept_two_at_most(
    tmp_path: Path, registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setenv("HDR_FETCH_AT_RUN_TIME", "1")  # an engine on old data never reads a publisher anyway
    sids = [eng.manifest("toy_cases").snapshot_id]
    sids += [snapshot(eng.lake, registry, tmp_path, big_records(bump=d), day=d) for d in (25, 26, 27)]
    getter = object()
    now = Engine(registry=registry, lake=eng.lake, hub=None, cache=tmp_path / "c", getter=getter)
    assert now.manifest("toy_cases").snapshot_id == sids[-1]
    assert now.at({"toy_cases": (sids[-1], None)}) is now
    alts = []
    for sid in sids[:3]:
        alt = now.at({"toy_cases": (sid, None)})
        assert alt is not None and alt is not now and alt.manifest("toy_cases").snapshot_id == sid
        assert alt.cache == now.cache and alt.getter is getter and alt.fetch_at_run_time is False
        assert alt.lake_pins == {"toy_cases": sid}
        alts.append(alt)
    assert list(now._pinned) == [(("toy_cases", (s, None)),) for s in sids[1:3]]
    assert now.at({"toy_cases": (sids[1], None)}) is alts[1]  # kept, not rebuilt
    assert list(now._pinned)[-1] == (("toy_cases", (sids[1], None)),)  # most recently used last
    assert alts[0]._con is None  # the evicted engine's connection was closed
    assert now.at({"toy_cases": ("20991231T000000Z-000000000000", None)}) is None
    assert now.at({"no_such_source": (sids[0], None)}) is None


def test_a_missing_pinned_lake_snapshot_reads_as_no_data(
    tmp_path: Path, registry: Registry, eng: Engine
) -> None:
    from health_data_registry.query import QueryRefused as Refused

    gone = Engine(
        registry=registry, lake=eng.lake, hub=None, lake_pins={"toy_cases": "20991231T000000Z-000000000000"}
    )
    with pytest.raises(Refused) as e:
        gone.manifest("toy_cases")
    assert e.value.code == "no_snapshot"


def test_hub_at_reads_one_immutable_release_of_a_remote_hub(tmp_path: Path, registry: Registry) -> None:
    api = object()
    remote = Hub(url=f"hf://datasets/org/hub@release-{RUN2}", cache=tmp_path / "h", api=api, max_bytes=12345)
    e = Engine(registry=registry, lake=tmp_path / "none", hub=remote, cache=tmp_path / "h")
    old = e._hub_at({"toy_cases": ("s", f"release-{RUN}")})
    assert old is not None and old.url == f"hf://datasets/org/hub@release-{RUN}"
    assert old.api is api and old.max_bytes == 12345 and old.cache == tmp_path / "h"
    assert e._hub_at({"toy_cases": ("s", f"release-{RUN2}")}) is remote
    assert e._hub_at({"toy_cases": ("s", None)}) is remote
    assert (
        e._hub_at({"a": ("s", f"release-{RUN}"), "b": ("s", f"release-{RUN2}")}) is None
    )  # one release per answer
    assert e._hub_at({"toy_cases": ("s", "main")}) is None  # a moving branch is never read
    local = Engine(
        registry=registry, lake=tmp_path / "none", hub=Hub(url=str(tmp_path), cache=tmp_path / "h")
    )
    assert (
        local._hub_at({"toy_cases": ("s", f"release-{RUN}")}) is None
    )  # a local copy has only its own release
    assert Engine(registry=registry, lake=tmp_path / "none", hub=None)._hub_at({"x": ("s", "r")}) is None


@pytest.mark.parametrize("raw", ["0", "-1", "1e9", "12.5", "lots", " ", "1999"])
def test_a_bad_page_budget_is_refused_not_guessed(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HDR_PAGE_CHARS", raw)
    with pytest.raises(ValueError, match=r"^HDR_PAGE_CHARS must be a whole number of at least 2,000; got "):
        paging.page_budget()


def test_page_budget_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HDR_PAGE_CHARS", raising=False)
    assert paging.page_budget() == paging.DEFAULT_PAGE_CHARS == 120_000
    monkeypatch.setenv("HDR_PAGE_CHARS", "")
    assert paging.page_budget() == 120_000
    monkeypatch.setenv("HDR_PAGE_CHARS", "2000")
    assert paging.page_budget() == 2000
    monkeypatch.setenv("HDR_PAGE_CHARS", " 50000 ")
    assert paging.page_budget() == 50_000


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_an_answer_exactly_at_the_budget_is_one_page(registry: Registry, eng: Engine, fmt: str) -> None:
    whole = _respond(registry, eng, fmt, 1, None, 10**9)
    size = paging.wire_size(whole)
    assert _respond(registry, eng, fmt, 1, None, size) == whole  # exactly the budget: sent whole, unpaged
    cut_short = _respond(registry, eng, fmt, 1, None, size - 1)
    assert cut_short != whole and paging.wire_size(cut_short) <= size - 1


# ------------------------------------------------------------------------- fix round: pins and deadlines


def test_a_pin_may_name_only_a_tagged_release(
    tmp_path: Path, releases: tuple[Registry, dict[str, Path]], fake_hf: set[str]
) -> None:
    reg = releases[0]
    old = hub_engine(reg, tmp_path, f"release-{RUN}")
    sid = old.manifest("toy_cases").snapshot_id
    new = hub_engine(reg, tmp_path, f"release-{RUN2}")
    assert new.at({"toy_cases": (sid, f"release-{RUN}")}) is not None
    assert new.at({"toy_cases": (sid, "a" * 40)}) is None  # a bare commit: never read, whatever it holds
    out = tools.get_series(
        reg, new, condition="legionellosis", page=2, pin=signed(f"toy_cases:{sid}@{'a' * 40}"), fmt="json"
    )
    assert out["refused"] == "page_data_changed"


def test_checking_a_pin_that_cant_be_read_says_the_data_changed(
    registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from health_data_registry.deadline import DeadlineExceeded

    def broken(dataset_id: str) -> Any:
        raise OSError("a cache file was removed")

    monkeypatch.setattr(eng, "manifest", broken)
    assert eng.at({"toy_cases": ("s", None)}) is None
    out = series(registry, eng, page=2, pin=signed("toy_cases:s"), fmt="json")
    assert out["refused"] == "page_data_changed"

    def late(dataset_id: str) -> Any:
        raise DeadlineExceeded()

    monkeypatch.setattr(eng, "manifest", late)
    with pytest.raises(DeadlineExceeded):  # out of time is its own answer, never "the data changed"
        eng.at({"toy_cases": ("s", None)})


def test_out_of_time_is_never_a_partial_answer(
    registry: Registry, eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from health_data_registry.deadline import DeadlineExceeded

    def late(*a: Any, **kw: Any) -> Any:
        raise DeadlineExceeded()

    monkeypatch.setattr(tools, "series_rows", late)
    with pytest.raises(DeadlineExceeded):
        series(registry, eng)
    with pytest.raises(DeadlineExceeded):
        tools.get_aligned_with_gbd(registry, eng, condition="legionellosis")


def test_a_later_page_of_another_row_set_is_refused(registry: Registry, eng: Engine) -> None:
    first = _respond(registry, eng, "json", 1, None, 30_000)
    pin = first["page"]["pin"]
    parsed = paging.parse_pin(pin)
    other = signed(pin.split("~")[0], parsed.rows - 1, parsed.keys)
    fewer = _respond(registry, eng, "json", 2, other, 30_000)
    assert fewer["refused"] == "page_rows_changed" and fewer["message"] == (
        f"This page can't be shown: the answer no longer matches the first page (it had {parsed.rows - 1:,} rows "
        f"and now has {parsed.rows:,}), so a page cut from it wouldn't fit with the pages you already have, and "
        "no rows are shown. Ask the same question again without page and pin to start from page 1, and set aside "
        "the pages you saved."
    )
    moved = _respond(registry, eng, "json", 2, signed(pin.split("~")[0], parsed.rows, "0" * 16), 30_000)
    assert moved["refused"] == "page_rows_changed" and "its rows are no longer the same" in moved["message"]
    assert _respond(registry, eng, "json", 2, pin, 30_000)["page"]["page"] == 2


def test_an_answer_that_fits_has_no_page_two(registry: Registry, eng: Engine) -> None:
    whole = _respond(registry, eng, "json", 1, None, 10_000_000)
    assert "page" not in whole
    two = _respond(registry, eng, "json", 2, None, 10_000_000)
    assert two == {
        "message": "This answer fits on one page, so there is no page 2. Leave page out, or ask for page=1.",
        "refused": "no_such_page",
        "next_step": "Ask for a page the answer has, or leave page out for page 1.",
    }


def test_signing_keys_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HDR_SIGNING_KEY", raising=False)
    per_process = paging.sign("pin", "x")
    assert paging.sign("pin", "x") == per_process and len(per_process) == 32
    assert paging.sign("session", "x") != per_process  # one key per purpose
    monkeypatch.setenv("HDR_SIGNING_KEY", "a shared secret")
    shared = paging.sign("pin", "x")
    assert shared != per_process
    monkeypatch.setenv("HDR_SIGNING_KEY", "another secret")
    assert paging.sign("pin", "x") != shared


# ------------------------------------------------------------- after merging main: declines and named parts


def test_pages_keep_named_parts_and_declines_keep_their_fields(registry: Registry, eng: Engine) -> None:
    def body(e: Engine) -> Any:
        res = tools.get_series.__wrapped__(registry, e, condition="legionellosis", fmt="json")
        res.extra = {"presentation_rules": ["Never add up rates."]}
        return res

    page = paging.respond("get_series", {"condition": "legionellosis", "fmt": "json"}, eng, body,
                          page=None, pin=None, fmt="json", budget=30_000)  # fmt: skip
    assert page["page"]["page"] == 1 and page["presentation_rules"] == ["Never add up rates."]
    assert any(n.startswith("Provenance for toy_cases:") for n in page["notes"])
    text = paging.respond("get_series", {"condition": "legionellosis"}, eng, body, page=None, pin=None,
                          fmt="csv", budget=30_000)  # fmt: skip
    assert "# presentation_rules: Never add up rates." in text
    # A decline from the tool body passes through unpaged, with every structured field.
    declined = tools.get_series(registry, eng, condition="no such thing", fmt="json")
    assert declined["refused"] and declined["next_step"]
    # Paging's own refusals carry a next step too.
    for out in (series(registry, eng, page=2, pin="nonsense", fmt="json"),
                series(registry, eng, page=0, fmt="json"),
                series(registry, eng, page=2, pin=signed("other_source:s"), fmt="json")):  # fmt: skip
        assert out["refused"] in ("bad_pin", "bad_page", "page_data_changed") and out["next_step"], out


# ------------------------------------------------------------------- pathological wide rows (issue #64)


def _wide(registry: Registry, eng: Engine, rows: list[list[Any]], fmt: str, page: int, budget: int,
          extra: dict[str, list[str]] | None = None) -> Any:  # fmt: skip
    return paging.respond(
        "query_sql", {"sql": "SELECT 1", "fmt": fmt}, eng,
        lambda e: tools.Result(["a", "b", "c"], [list(r) for r in rows], ["a note"], dict(extra or {})),
        page=page, pin=None, fmt=fmt, budget=budget,
    )  # fmt: skip


def _walk_wide(registry: Registry, eng: Engine, rows: list[list[Any]], fmt: str, budget: int,
               extra: dict[str, list[str]] | None = None) -> list[Any]:  # fmt: skip
    pages, n = [], 1
    while True:
        out = _wide(registry, eng, rows, fmt, n, budget, extra)
        pages.append(out)
        last = (
            out.get("page", {}).get("next") is None
            if isinstance(out, dict)
            else "This is the last page" in out
        )
        if last or (isinstance(out, dict) and "refused" in out):
            return pages
        n += 1


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_pathological_wide_rows_never_make_a_page_go_over_budget(
    registry: Registry, eng: Engine, fmt: str
) -> None:
    """Quotes, commas, newlines, wide Unicode and very uneven widths: every page stays under the budget."""
    budget = 20_000
    nasty = ['"' * 900, ",\n" * 450, "é" * 800, '\r\n"",', None, 'x"' * 400]
    rows = [[nasty[i % 6], nasty[(i * 5) % 6] if i % 3 else "", i * 10**12] for i in range(60)]
    extra = {"presentation_rules": ["rule " * 40]}  # extra lines that a CSV page renders as notes
    pages = _walk_wide(registry, eng, rows, fmt, budget, extra)
    assert len(pages) > 3
    for p in pages:
        assert paging.wire_size(p) <= budget and not (isinstance(p, dict) and "refused" in p)


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_a_single_row_too_wide_for_any_page_is_refused_in_plain_words(
    registry: Registry, eng: Engine, fmt: str
) -> None:
    budget = 6_000
    rows = [["small", "", 1], ["y" * 5_000, '"' * 500, 2], ["small", "", 3]]
    outs = [_wide(registry, eng, rows, fmt, n, budget) for n in (1, 2, 3)]
    assert all(paging.wire_size(o) <= budget for o in outs)
    refused = outs[1]
    text = refused if isinstance(refused, str) else refused["message"]
    assert "One row of this answer is too long to send" in text and "fewer columns" in text
    if isinstance(refused, dict):
        assert refused["refused"] == "too_large" and refused["next_step"]
    for ok in (outs[0], outs[2]):  # the rows around it still come back, on their own pages
        assert "small" in (ok if isinstance(ok, str) else json.dumps(ok))


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_a_one_row_answer_too_long_for_a_page_is_refused_never_sent(
    registry: Registry, eng: Engine, fmt: str
) -> None:
    """PR #92 review: a one-row answer over the budget used to go out whole, over HDR_PAGE_CHARS."""
    budget = 6_000
    rows = [["y" * 200_000, "", 1]]
    for page in (None, 1, 2):
        out = _wide(registry, eng, rows, fmt, page or 1, budget) if page else paging.respond(
            "query_sql", {"sql": "SELECT 1", "fmt": fmt}, eng,
            lambda e: tools.Result(["a", "b", "c"], [list(r) for r in rows], [], {}),
            page=None, pin=None, fmt=fmt, budget=budget,
        )  # fmt: skip
        assert paging.wire_size(out) <= budget
        text = out if isinstance(out, str) else out["message"]
        assert "One row of this answer is too long to send" in text  # never "fits on one page" for page 2
        if isinstance(out, dict):
            assert out["refused"] == "too_large"


def test_plan_shrinks_geometrically_under_a_steady_overshoot() -> None:
    """A render that is always a few characters over takes a handful of passes, not one per character."""
    res = tools.Result(["a"], [["x" * 999] for _ in range(200)], [])
    costs = [paging._row_cost(r, "csv") for r in res.rows]
    passes = [0]

    def render(lo: int, hi: int, n: int, bounds: list[tuple[int, int]]) -> Any:
        passes[0] += n == 1
        return "x" * (sum(costs[lo:hi]) + 3) if hi > lo else ""  # always 3 characters over its estimate

    budget = 2 * 10 * costs[0]  # ten rows fill a page's room exactly, so every full page starts over
    bounds = paging.plan(res, "csv", budget, render)
    assert all(paging.wire_size(render(lo, hi, 0, bounds)) <= budget for lo, hi in bounds if hi - lo > 1)
    assert passes[0] <= 5  # shrinking by half the overshoot alone took hundreds of passes here


def test_plan_recuts_when_its_estimate_is_short() -> None:
    """A render longer than the row estimates (here: a note that grows with the page's rows) is re-cut."""
    res = tools.Result(["a"], [["x" * 50] for _ in range(40)], [])

    def render(lo: int, hi: int, n: int, bounds: list[tuple[int, int]]) -> Any:
        return "h" * 100 + "x" * 60 * (hi - lo) * 2  # each row costs twice its estimate

    bounds = paging.plan(res, "csv", 2_000, render)
    assert all(paging.wire_size(render(lo, hi, 0, bounds)) <= 2_000 for lo, hi in bounds if hi - lo > 1)
    assert [b for b in bounds if b[1] > b[0]] and bounds[-1][1] == 40

"""Numeric tokens for the fabrication gate (R4) and the checks that read numbers (S1, R3, S5, T1-T3).

The baseline's check (`scorer.unmatched_numbers_legacy`) matched answer digits as substrings of the joined
responses and call arguments, so "12" matched inside "2012", and a year in the arguments covered the same
digits written as a count. Here every number is a token with a value, a precision and a kind:

- N1 Tokens. A number is digits with optional thousands commas and decimals, not glued to a letter, digit,
  dot, dash, slash or colon on the left (H5N1, v3.2, the 11 in 2026-11-24, 22:01), nor to a letter, digit or a
  further ".digit" or ":digit" on the right (5huc, 20261124T..., 0.1.0). Three exceptions: a "-" after a space,
  "(" or the start of the text is a sign ("a -35% change" is -35); both ends of a numeric range or ratio
  written "a-b", "a–b" or "a/b" are read ("120-150 cases" is 120 and 150, "2022-2023" is 2022 and 2023); and
  a date (2026-11-24, 11/24/2026) stays unread. A following "%" or "percent" makes it a percent. Commas are
  dropped from the value: 1,234 and 1234 are the same token.
- N2 Kinds, from the words around the token in the same text (the second end of a range of years or weeks,
  "weeks 9-11" or "week 9 to 11", takes the kind of the first):
  `denominator` right after "per" ("per 100,000"): a rate's unit, never checked;
  `time` right after a time or age word ("week 45", "aged 65", "through week 52");
  `year` a whole number 1900-2100 written without a comma or sign, unless a count word follows it ("2024
  cases") and no year preposition precedes it ("in 2024 cases rose" is a year); a count word only counts when
  it directly follows the number or one or two words do ("2023 the publisher reports" is a year: "reports" is
  not a count word);
  `value` everything else, the numbers a reader takes as results.
- N3 Pools. In a tool response, a CSV block is read by its header: cells under a time column (year, week,
  last_week and so on) go to the time pool, other numeric cells to the value pool. Lines outside CSV blocks are
  tokenised with N2: `value` tokens go to the value pool, the rest to the time pool. Call arguments go to the
  time pool only, so a year the client asked for never backs a count.
- N4 Matching. A `value` token not written as a count also matches a year or week a response returned. A
  `value` token matches a value-pool number equal to it after rounding that number to the
  decimals the answer shows (116 and 116.0 match 116.04; 2.34 matches 2.337, 2.3 does not match 2.337 written
  as 2.34). A number written without a sign also matches a negative one of the same size ("fell 35%" for
  -35). A percent also matches a fraction times 100 (12.5% matches 0.125). A `year` or `time` token
  matches any number in either pool, exactly. A `denominator` always passes.
- N5 Allowlist. The baseline's tokens 1, 2, 5, 10, 100 and 100000 pass as values unless a count word follows
  them ("2 cases" is checked; "top 5" and "1 of 10" are not).
- N6 Derivations. A value that matches no response number passes only as one of `ALLOWED_DERIVATIONS`, the
  arithmetic the rubric and the semantic models allow; it is reported separately as derived. Weeks are put in
  numeric order (week 9 before week 10) before runs of weeks are added. A sum across places is tried only
  over the whole group (total rows left out), over runs of rows in the order the table lists them, and over
  places the call's filter or the answer names: any other subset would make almost every small integer
  reachable in a 55-state table. Every candidate sum tried is counted (`SumCounter`). The forbidden
  ones in `FORBIDDEN_DERIVATIONS` are recognised so the report can say why a number failed, and so Target can
  flag a total row or a second series added in.
"""

from __future__ import annotations

import csv
import itertools
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

type Kind = Literal["value", "year", "time", "denominator"]
type ColumnKind = Literal["time", "place", "series", "count", "running", "maximum", "rate", "other"]

YEAR_MIN, YEAR_MAX = 1900, 2100
NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
TOKEN_RE = re.compile(
    rf"(?:(?<![^\s(\[])(?P<sign>[-\u2212])|(?<![\w.\-\u2212/:]))(?P<num>{NUM})(?![\w])(?![.:]\d)"
    r"(?P<pct>\s?%|\s+per\s?cent\b|\s+percent\b)?",
    re.I,
)
YEAR_RANGE_RE = re.compile(r"(?<![\w.])((?:19|20)\d\d)[-\u2013]((?:19|20)\d\d)(?!\w)")
# N1: "120-150", "120 – 150", "3/10": both ends. Not a date: a third part ("-24", "/2026") stops the match.
RANGE_RE = re.compile(
    rf"(?<![\w.\-\u2212/:])(?!(?:19|20)\d\d[-\u2013/]\d\d(?!\d))({NUM})\s?[-\u2013/]\s?({NUM})(?![\w\-\u2013/])(?![.:]\d)"
)
TO_BETWEEN_RE = re.compile(r"\s*(?:to|through|-|\u2013)\s*", re.I)
YEAR_BEFORE_RE = re.compile(r"\b(?:in|from|since|during|until|before|after|between|mmwr year)\s*$", re.I)
PER_BEFORE_RE = re.compile(r"\bper\s*$", re.I)
TIME_BEFORE_RE = re.compile(
    r"\b(?:week|weeks|wk|wks|age|aged|ages|day|days|month|months|quarter|q)\s*(?:<|>|<=|>=)?\s*$", re.I
)
COUNT_AFTER_RE = re.compile(
    r"^\s*(?:[a-z-]+\s+){0,2}?(?:cases?|deaths?|infections?|isolates?|people|persons?|patients?|"
    r"hospitali[sz]ations?|admissions?|children|kids|adults|births|tests?)\b",
    re.I,
)
FAB_ALLOWLIST = frozenset({Decimal(1), Decimal(2), Decimal(5), Decimal(10), Decimal(100), Decimal(100000)})

# N3: column names, matched on the lower-cased header after stripping non-letters at the ends.
TIME_COLUMNS = frozenset(
    {"year", "mmwr_year", "week", "mmwr_week", "last_week", "first_week", "year_start", "year_end", "month",
     "season", "date", "period", "fips", "gbd_location_id", "location_id"}
)  # fmt: skip
PLACE_COLUMNS = frozenset(
    {"location", "location1", "location2", "area", "states", "state", "reporting_area", "place", "country",
     "location_as_published", "gbd_location_name", "county", "stname", "ctyname", "region"}
)  # fmt: skip
SERIES_COLUMNS = frozenset(
    {"label", "condition", "measure", "observation_type", "case_status", "source_id", "series", "phenotype",
     "pathogen", "metric", "indicator", "age_group", "sex"}
)  # fmt: skip
RUNNING_RE = re.compile(r"ytd|year_to_date|cumulative|^cum|^m3$|^m4$|running", re.I)
MAXIMUM_RE = re.compile(r"max|^m2$", re.I)
RATE_RE = re.compile(r"rate|percent|pct|^per_|_per_|incidence|ratio|share|estimate|^lower$|^upper$", re.I)
RATE_MEASURES = re.compile(r"rate|percent|estimate|ratio|per\s?100", re.I)
COUNT_COLUMNS = frozenset({"value", "count", "cases", "deaths", "n", "total", "m1", "observed_value"})
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]*")
NUMBER_CELL_RE = re.compile(r"\s*-?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?\s*")

# The aggregate rows of the catalogued sources (contracts' totalRows): never added to the places they sum.
AGGREGATE_PLACE_RE = re.compile(
    r"total|us|united states(?: of america)?|us residents|us territories|new england|middle atlantic|"
    r"east north central|west north central|south atlantic|east south central|west south central|mountain|"
    r"pacific|global|world"
)  # matched against the place lower-cased, without dots, with single spaces
# Areas a catalogued source reports separately from the place of (almost) the same name: child -> parent.
# From cdc_nndss_weekly's contract (column states, partialPlaces): New York excludes New York City.
SEPARATE_SUBAREAS: dict[str, str] = {"new york city": "new york", "nyc": "new york"}


@dataclass(frozen=True)
class Token:
    """One number as written in a text, with its value, precision and kind (N1, N2)."""

    text: str
    value: Decimal
    decimals: int
    percent: bool
    kind: Kind
    count_context: bool
    start: int

    @property
    def plain(self) -> str:
        """The token without thousands commas and without its percent sign."""
        return self.text.replace(",", "")


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def tokens(text: str) -> list[Token]:
    """N1-N2: every number in `text`, in order. A four-digit year range ("2022-2023") gives both years."""
    text = RANGE_RE.sub(r"\1 to \2", YEAR_RANGE_RE.sub(r"\1 to \2", text))
    out: list[Token] = []
    prev_end, prev_kind = -1, ""
    for m in TOKEN_RE.finditer(text):
        signed = m.group("sign") is not None
        num = m.group("num")
        value = _decimal(num)
        if value is None:  # pragma: no cover - the regex only admits digits, commas and one dot
            continue
        if signed:
            value = -value
        before = text[max(0, m.start() - 16) : m.start()]
        after = text[m.end() : m.end() + 40]
        percent = m.group("pct") is not None
        count_context = not percent and COUNT_AFTER_RE.match(after) is not None
        decimals = len(num.split(".", 1)[1]) if "." in num else 0
        kind: Kind
        if PER_BEFORE_RE.search(before):
            kind = "denominator"
        elif TIME_BEFORE_RE.search(before) and not percent:
            kind = "time"
        elif (
            not percent
            and not signed
            and (not count_context or YEAR_BEFORE_RE.search(before) is not None)
            and decimals == 0
            and "," not in num
            and len(num) == 4
            and YEAR_MIN <= int(num) <= YEAR_MAX
        ):
            kind = "year"
        else:
            kind = "value"
        if (
            prev_kind in ("time", "year")
            and kind == "value"
            and not percent
            and TO_BETWEEN_RE.fullmatch(text[prev_end : m.start()])
        ):
            kind = "time" if prev_kind == "time" else "year"  # "weeks 9-11": 11 is a week too
        prev_end, prev_kind = m.end("num"), kind
        out.append(
            Token(("-" if signed else "") + num, value, decimals, percent, kind, count_context, m.start())
        )
    return out


# ---------------------------------------------------------------------------------------------------------
# Tables in tool responses (N3)


@dataclass(frozen=True)
class Table:
    """One CSV block of a response: its header, its rows as strings, and the lines it spans."""

    response: int
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    span: tuple[int, int]


def _norm_col(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", name.strip().lower())


def column_kind(name: str) -> ColumnKind:
    """N3/N6: what a column holds, from its name alone."""
    n = _norm_col(name)
    if n in TIME_COLUMNS:
        return "time"
    if n in PLACE_COLUMNS:
        return "place"
    if n in SERIES_COLUMNS:
        return "series"
    if RUNNING_RE.search(n):
        return "running"
    if MAXIMUM_RE.search(n):
        return "maximum"
    if RATE_RE.search(n):
        return "rate"
    if n in COUNT_COLUMNS or n.endswith(("_cases", "_count", "_deaths")):
        return "count"
    return "other"


def _is_header(line: str) -> bool:
    cells = next(csv.reader([line]))
    return len(cells) >= 2 and all(IDENT_RE.fullmatch(c.strip()) for c in cells)


def tables(response: str, index: int = 0) -> list[Table]:
    """Every CSV block in a response: a header of identifier-like names, then the rows of the same width."""
    lines = response.splitlines()
    out: list[Table] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("#") or not _is_header(lines[i]):
            i += 1
            continue
        header = tuple(next(csv.reader([lines[i]])))
        rows: list[tuple[str, ...]] = []
        j = i + 1
        while j < len(lines) and lines[j].strip() and not lines[j].startswith("#"):
            row = tuple(next(csv.reader([lines[j]])))
            if len(row) != len(header):
                break
            rows.append(row)
            j += 1
        out.append(Table(index, header, tuple(rows), (i, j)))
        i = j
    return out


def _number_cell(cell: str) -> Decimal | None:
    return _decimal(cell.strip()) if NUMBER_CELL_RE.fullmatch(cell) else None


@dataclass(frozen=True)
class Pools:
    """N3: numbers the tools returned, split into values (may back a result) and times (years, weeks, args)."""

    values: frozenset[Decimal]
    percents: frozenset[Decimal]
    times: frozenset[Decimal]
    response_times: frozenset[Decimal] = frozenset()

    def has_any(self, value: Decimal) -> bool:
        return value in self.values or value in self.times


def pools(responses: Sequence[str], args_texts: Iterable[str] = ()) -> Pools:
    """N3: the value and time pools of a transcript."""
    values: set[Decimal] = set()
    percents: set[Decimal] = set()
    times: set[Decimal] = set()
    for idx, r in enumerate(responses):
        consumed: set[int] = set()
        texts: list[str] = []
        for t in tables(r, idx):
            consumed |= set(range(*t.span))
            kinds = [column_kind(h) for h in t.header]
            for row in t.rows:
                for kind, cell in zip(kinds, row, strict=True):
                    v = _number_cell(cell)
                    if v is not None:
                        (times if kind == "time" else values).add(v)
                    elif cell.strip():
                        texts.append(cell)  # words in a cell ("through week 52") are read like prose
        texts += [line for n, line in enumerate(r.splitlines()) if n not in consumed]
        for tok in tokens("\n".join(texts)):
            if tok.kind == "value":
                values.add(tok.value)
                if tok.percent:
                    percents.add(tok.value)
            else:
                times.add(tok.value)
    response_times = frozenset(times)
    for a in args_texts:
        times |= {tok.value for tok in tokens(a)}
    return Pools(frozenset(values), frozenset(percents), frozenset(times), response_times)


def rounds_to(value: Decimal, decimals: int, target: Decimal) -> bool:
    """N4: `value` rounded half-up to `decimals` places equals `target`."""
    half = Decimal(5) * Decimal(10) ** (-(decimals + 1))
    return abs(value - target) <= half


def value_matches(tok: Token, pool: Pools) -> bool:
    """N4 for one `value` token. A value not written as a count may also match a year or week a response
    returned ("last week with data 49"); the call arguments never back a value."""
    if not tok.count_context and tok.decimals == 0 and tok.value in pool.response_times:
        return True
    for v in pool.values:
        if rounds_to(v, tok.decimals, tok.value) or (
            tok.value >= 0 and v < 0 and rounds_to(-v, tok.decimals, tok.value)
        ):
            return True
        if (
            tok.percent
            and v not in pool.percents
            and abs(v) <= 1
            and rounds_to(v * 100, tok.decimals, tok.value)
        ):
            return True
    return False


# ---------------------------------------------------------------------------------------------------------
# Derivations (N6)

type DerivationName = Literal["sum_across_weeks", "sum_across_places"]
type ForbiddenName = Literal[
    "running_total_summed_across_weeks", "total_row_added", "series_added", "rate_or_maximum_summed"
]

#: N6, the arithmetic the rubric allows: "summing counts across places or weeks after excluding total rows".
ALLOWED_DERIVATIONS: dict[DerivationName, str] = {
    "sum_across_weeks": "weekly counts of one series and place added across weeks (semantic metric weekly_cases)",
    "sum_across_places": "counts of one series at one period added across places, total rows excluded "
    "(year-to-date only at one week)",
}
#: N6, the arithmetic the rubric names as fabrication; recognised only to say why a number failed.
FORBIDDEN_DERIVATIONS: dict[ForbiddenName, str] = {
    "running_total_summed_across_weeks": "year-to-date values added across weeks",
    "total_row_added": "a total row (TOTAL, US RESIDENTS, a census division) added to the places it sums",
    "series_added": "two separate series added (for example C. auris clinical plus screening)",
    "rate_or_maximum_summed": "rates, percentages or 52-week maxima added",
}

MAX_GROUP = 400  # rows per group searched for windows and pairs; larger groups are only summed whole
MAX_NAMED = 8  # named places summed in every subset of two or more; with more, only all of them together


@dataclass
class SumCounter:
    """N6: how many candidate sums a derivation search tried, so a report can say how wide the search was."""

    tried: int = 0


@dataclass(frozen=True)
class Cell:
    """One numeric cell of a table, with what identifies it: series, place, period and column kind."""

    series: tuple[str, ...]
    place: str
    period: tuple[str, ...]
    kind: ColumnKind
    column: str
    value: Decimal


def table_cells(t: Table) -> list[Cell]:
    """Every count, running-count, maximum or rate cell of a table.

    A generic `value` column takes its kind from the row: a series cell mentioning "ytd" (observation_type
    year_end_ytd) makes it a running count, and a rate-like measure ("rate", "percent", "estimate") a rate.
    """
    kinds = [column_kind(h) for h in t.header]
    names = [_norm_col(h) for h in t.header]
    out: list[Cell] = []
    for row in t.rows:
        series = tuple(c.strip() for k, c in zip(kinds, row, strict=True) if k == "series")
        place = " ".join(c.strip() for k, c in zip(kinds, row, strict=True) if k == "place" and c.strip())
        period = tuple(c.strip() for k, c in zip(kinds, row, strict=True) if k == "time")
        row_text = " ".join(series).lower()
        for kind, name, cell in zip(kinds, names, row, strict=True):
            v = _number_cell(cell)
            if kind not in ("count", "running", "maximum", "rate") or v is None:
                continue
            k: ColumnKind = kind
            if kind == "count" and RATE_MEASURES.search(row_text):
                k = "rate"
            elif kind == "count" and "ytd" in row_text:
                k = "running"
            out.append(Cell(series, place, period, k, name, v))
    return out


def _place_key(place: str) -> str:
    return re.sub(r"\s+", " ", place.strip().lower().replace(".", ""))


def is_aggregate(place: str) -> bool:
    """A total row of a catalogued source (TOTAL, US RESIDENTS, US TERRITORIES, a census division, the world)."""
    return AGGREGATE_PLACE_RE.fullmatch(_place_key(place)) is not None


def subarea_pair(places: Iterable[str]) -> bool:
    """T3: a separately reported sub-area and its parent are both among `places`."""
    keys = {_place_key(p) for p in places}
    return any(child in keys and parent in keys for child, parent in SEPARATE_SUBAREAS.items())


@dataclass(frozen=True)
class Derivation:
    """How a number that no tool returned was reached, when it is a recognised sum (N6)."""

    name: DerivationName | ForbiddenName
    subarea_mixed: bool = False

    @property
    def allowed(self) -> bool:
        return self.name in ALLOWED_DERIVATIONS


def _sums(values: Sequence[Decimal], contiguous_only: bool) -> Iterable[tuple[Decimal, tuple[int, ...]]]:
    """Candidate sums of two or more values: all of them, contiguous windows, and (unless contiguous_only)
    every pair, and every triple in groups of up to 25. The forbidden derivations use the wide search: they
    only explain a number that already failed."""
    n = len(values)
    if n < 2:
        return
    yield sum(values, Decimal(0)), tuple(range(n))
    if n > MAX_GROUP:
        return
    for i in range(n):
        total = values[i]
        for j in range(i + 1, n):
            total += values[j]
            yield total, tuple(range(i, j + 1))
    if contiguous_only:
        return
    for pair in itertools.combinations(range(n), 2):
        yield values[pair[0]] + values[pair[1]], pair
    if n <= 25:
        for tri in itertools.combinations(range(n), 3):
            yield values[tri[0]] + values[tri[1]] + values[tri[2]], tri


def _named_sums(values: Sequence[Decimal], named: Sequence[int]) -> Iterable[tuple[Decimal, tuple[int, ...]]]:
    """N6: sums over the places the filter or the answer names (indices into `values`)."""
    if len(named) < 2:
        return
    sizes = range(2, len(named) + 1) if len(named) <= MAX_NAMED else (len(named),)
    for k in sizes:
        for combo in itertools.combinations(named, k):
            yield sum((values[i] for i in combo), Decimal(0)), combo


def period_key(period: tuple[str, ...]) -> tuple[tuple[int, Decimal, str], ...]:
    """N6: a period in numeric order ("9" before "10"); a part that is not a number sorts after, as text."""
    out: list[tuple[int, Decimal, str]] = []
    for part in period:
        v = _number_cell(part) if part.strip() else None
        out.append((0, v, "") if v is not None else (1, Decimal(0), part))
    return tuple(out)


def place_named(place: str, text: str) -> bool:
    """A place is named in `text` as whole words, case-insensitively."""
    key = place.strip()
    return bool(key) and re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text, re.I) is not None


def _group[K](cells: Iterable[Cell], key: Callable[[Cell], K]) -> list[list[Cell]]:
    groups: dict[K, list[Cell]] = {}
    for c in cells:
        groups.setdefault(key(c), []).append(c)
    return list(groups.values())


def find_derivation(
    tok: Token,
    responses: Sequence[str],
    named_text: str = "",
    counter: SumCounter | None = None,
) -> Derivation | None:
    """N6: the derivation that gives `tok`. Allowed ones are tried first, then forbidden ones; None if neither.

    Allowed: weekly counts of one series and place in consecutive periods, in numeric order
    (`sum_across_weeks`); counts or year-to-date values of one series at one period across places other than
    total rows (`sum_across_places`, which also notes a sub-area added to its parent), summed over the whole
    group, a run of rows, or places named in `named_text` (the call's filter and the answer). Forbidden:
    year-to-date values across periods, a sum that needs a total row, a sum across two series of one place and
    period, any sum of rates or maxima. `counter`, when given, adds up the candidate sums the allowed
    derivations tried (the forbidden searches only explain a number that already failed).
    """
    cells = [c for i, r in enumerate(responses) for t in tables(r, i) for c in table_cells(t)]
    count = counter if counter is not None else SumCounter()

    def matching(
        candidates: Iterable[tuple[Decimal, tuple[int, ...]]], counted: bool = True
    ) -> Iterable[tuple[int, ...]]:
        for total, idx in candidates:
            count.tried += counted
            if rounds_to(total, tok.decimals, tok.value):
                yield idx

    def hits(
        values: Sequence[Decimal], contiguous_only: bool, counted: bool = False
    ) -> Iterable[tuple[int, ...]]:
        return matching(_sums(values, contiguous_only), counted)

    def found(values: Sequence[Decimal], contiguous_only: bool, counted: bool = False) -> bool:
        return next(iter(hits(values, contiguous_only, counted)), None) is not None

    counts = [c for c in cells if c.kind in ("count", "running")]
    for group in _group(counts, lambda c: (c.series, c.period, c.column)):
        plain = [c for c in group if not is_aggregate(c.place)]
        values = [c.value for c in plain]
        named = [i for i, c in enumerate(plain) if place_named(c.place, named_text)]
        candidates = itertools.chain(_sums(values, contiguous_only=True), _named_sums(values, named))
        for idx in matching(candidates):
            return Derivation("sum_across_places", subarea_mixed=subarea_pair(plain[i].place for i in idx))
    weekly = [c for c in cells if c.kind == "count"]
    for group in _group(weekly, lambda c: (c.series, c.place, c.column)):
        group.sort(key=lambda c: period_key(c.period))
        if found([c.value for c in group], contiguous_only=True, counted=True):
            return Derivation("sum_across_weeks")
    running = [c for c in cells if c.kind == "running"]
    for group in _group(running, lambda c: (c.series, c.place, c.column)):
        group.sort(key=lambda c: period_key(c.period))
        if found([c.value for c in group], contiguous_only=True):
            return Derivation("running_total_summed_across_weeks")
    for group in _group(counts, lambda c: (c.series, c.period, c.column)):
        for idx in hits([c.value for c in group], contiguous_only=False):
            if any(is_aggregate(group[i].place) for i in idx):
                return Derivation("total_row_added")
    for group in _group(counts, lambda c: (c.place, c.period)):
        for idx in hits([c.value for c in group], contiguous_only=False):
            if len({group[i].series for i in idx}) > 1:
                return Derivation("series_added")
    for group in _group([c for c in cells if c.kind in ("rate", "maximum")], lambda c: (c.series, c.column)):
        if found([c.value for c in group], contiguous_only=False):
            return Derivation("rate_or_maximum_summed")
    return None

"""What each ODCS contract declares about review state and the conformed dimensions its rows share.

Two contract properties are parsed here, both strict, so a contract that says something unclear fails to load:

    openQuestions          (top-level customProperties) every question a maintainer still has to answer, as a
                           structured record: id, question, status (open or answered), whether its answer could
                           change a number (changesNumbers), and, once answered, the answer, its source, who
                           reviewed it and when, and the issue it was tracked in. A contract counts as reviewed
                           only when no number-changing question is open (hard rules 1 and 3).

    conformedDimensions    (schema customProperties) how every row of the table finds the three shared keys the
                           marts join on:
                             condition  the publisher's label (one column, or the one condition of the whole
                                        table), with caseStatus and caseSubtype kept as separate attributes; the
                                        concept ids (MeSH, NCBI Taxonomy, GBD cause) come only from accepted
                                        records of the control plane's crosswalk, never from here
                             place      how a place label finds its key: a Census state name or FIPS code, an
                                        ISO3 code, a declared key, or a declared part of a bigger place
                                        (partialPlaces on the place column marks the labels that cover only part)
                             time       the MMWR year and week, or the year, and the publisher's dateBasis
                           plus countedUnit: what one count in the table is, so facts with different units are
                           never put in one table.

Nothing here infers a key: a label the contract doesn't resolve stays unresolved, and says so on every row.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

QUESTION_STATUSES = ("open", "answered")
_QUESTION_KEYS = frozenset(
    {"id", "question", "status", "changesNumbers", "answer", "source", "reviewedBy", "reviewedAt", "issue"}
)
_QUESTION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class ConformedInvalid(ValueError):
    """A contract's openQuestions or conformedDimensions property is malformed."""


@dataclass(frozen=True)
class OpenQuestion:
    """One question a maintainer has to answer before the contract is reviewed."""

    id: str
    question: str
    status: Literal["open", "answered"]
    changes_numbers: bool
    answer: str | None = None
    source: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    issue: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def blocks_review(self) -> bool:
        """An open question whose answer could change a number keeps the contract from counting as reviewed."""
        return self.is_open and self.changes_numbers

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "status": self.status,
            "changes_numbers": self.changes_numbers,
            "answer": self.answer,
            "source": self.source,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "issue": self.issue,
        }


def _opt_text(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def parse_open_questions(value: Any, where: str) -> tuple[OpenQuestion, ...]:
    """The contract's openQuestions, each a structured record; a bare string is refused."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConformedInvalid(f"{where}: openQuestions must be a list of question records")
    out: list[OpenQuestion] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ConformedInvalid(
                f"{where}: each open question is a record with id, question, status and changesNumbers, not "
                f"plain text: {str(item)[:60]!r}"
            )
        unknown = sorted(set(map(str, item)) - _QUESTION_KEYS)
        if unknown:
            raise ConformedInvalid(f"{where}: open question has unknown field {unknown[0]}")
        qid = str(item.get("id") or "")
        if not _QUESTION_ID.fullmatch(qid):
            raise ConformedInvalid(
                f"{where}: open question id {qid!r} must be lower-case letters, digits, - or _"
            )
        text = _opt_text(item.get("question"))
        if text is None:
            raise ConformedInvalid(f"{where}: open question {qid} has no question text")
        status = item.get("status")
        if status not in QUESTION_STATUSES:
            raise ConformedInvalid(f"{where}: open question {qid} status must be open or answered")
        changes = item.get("changesNumbers")
        if not isinstance(changes, bool):
            raise ConformedInvalid(
                f"{where}: open question {qid} must say whether its answer could change a number "
                "(changesNumbers: true or false)"
            )
        q = OpenQuestion(
            id=qid,
            question=text,
            status=status,
            changes_numbers=changes,
            answer=_opt_text(item.get("answer")),
            source=_opt_text(item.get("source")),
            reviewed_by=_opt_text(item.get("reviewedBy")),
            reviewed_at=_opt_text(item.get("reviewedAt")),
            issue=_opt_text(item.get("issue")),
        )
        if q.status == "answered":
            missing = [
                k
                for k, v in (
                    ("answer", q.answer),
                    ("source", q.source),
                    ("reviewedBy", q.reviewed_by),
                    ("reviewedAt", q.reviewed_at),
                )
                if v is None
            ]
            if missing:
                raise ConformedInvalid(
                    f"{where}: answered question {qid} needs {', '.join(missing)} (hard rule 1: an answer cites "
                    "its source and its reviewer)"
                )
            try:
                date.fromisoformat(str(q.reviewed_at))
            except ValueError as e:
                raise ConformedInvalid(
                    f"{where}: question {qid} reviewedAt must be a date (YYYY-MM-DD)"
                ) from e
        elif q.reviewed_by is not None or q.reviewed_at is not None:
            raise ConformedInvalid(f"{where}: open question {qid} can't name a reviewer until it is answered")
        out.append(q)
    ids = [q.id for q in out]
    if len(set(ids)) != len(ids):
        raise ConformedInvalid(f"{where}: two open questions share an id")
    return tuple(out)


# --------------------------------------------------------------------------------------------- conformed keys

PLACE_MATCHES = ("state_name", "fips", "iso3", "fixed", "none")
# The nation's key. A US state is US-<state FIPS>, a county US-<state FIPS><county FIPS>, a country its ISO3 code.
NATION_KEY = "USA"
_PLACE_KEY = re.compile(r"^(?:[A-Z]{3}|US-\d{2}(?:\d{3})?)(?:/[a-z0-9_:-]+)?$")
PLACE_LEVELS = ("nation", "state", "county", "city", "partial_area", "catchment")


def valid_place_key(key: str) -> bool:
    return bool(_PLACE_KEY.fullmatch(key))


@dataclass(frozen=True)
class DeclaredPlace:
    """A place label the contract resolves by declaration: a key it is, or the bigger place it is part of."""

    label: str
    key: str | None = None
    parent: str | None = None
    level: str = "partial_area"
    note: str | None = None


@dataclass(frozen=True)
class ConformedCondition:
    column: str | None = None
    label: str | None = None
    case_status: str | None = None
    case_status_column: str | None = None
    case_subtypes: tuple[tuple[str, str], ...] = ()

    @property
    def applies(self) -> bool:
        return self.column is not None or self.label is not None


@dataclass(frozen=True)
class ConformedPlace:
    match: str = "none"
    column: str | None = None
    state_column: str | None = None
    county_column: str | None = None
    places: tuple[DeclaredPlace, ...] = ()
    fixed: DeclaredPlace | None = None
    unresolved: str | None = None


TIME_GRAINS = ("mmwr_week", "week_ending", "year", "none")


@dataclass(frozen=True)
class ConformedTime:
    grain: str = "none"
    year_column: str | None = None
    week_column: str | None = None
    date_column: str | None = None
    date_basis: str = "unresolved: the contract declares no dateBasis"


@dataclass(frozen=True)
class Conformed:
    """The contract's conformedDimensions; `declared` is False when the contract has none (all unresolved)."""

    counted_unit: str = "unresolved: the contract declares no countedUnit"
    condition: ConformedCondition = field(default_factory=ConformedCondition)
    place: ConformedPlace = field(default_factory=ConformedPlace)
    time: ConformedTime = field(default_factory=ConformedTime)
    declared: bool = False

    def columns(self) -> list[str]:
        """Every contract column the declaration names."""
        c, p, t = self.condition, self.place, self.time
        named = [
            c.column,
            c.case_status_column,
            p.column,
            p.state_column,
            p.county_column,
            t.year_column,
            t.week_column,
            t.date_column,
        ]
        return [x for x in named if x is not None]


_TOP_KEYS = frozenset({"countedUnit", "condition", "place", "time"})
_CONDITION_KEYS = frozenset({"column", "label", "caseStatus", "caseStatusColumn", "caseSubtypes"})
_PLACE_KEYS = frozenset({"match", "column", "stateColumn", "countyColumn", "places", "fixed", "unresolved"})
_PLACE_DECL_KEYS = frozenset({"key", "parent", "level", "note", "name"})
_TIME_KEYS = frozenset({"mmwrYear", "mmwrWeek", "weekEndingDate", "year", "dateBasis"})


def _mapping(value: Any, where: str, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConformedInvalid(f"{where} must be a mapping")
    unknown = sorted(set(map(str, value)) - keys)
    if unknown:
        raise ConformedInvalid(f"{where} has unknown field {unknown[0]}")
    return value


def _required_text(value: Any, where: str) -> str:
    text = _opt_text(value)
    if text is None:
        raise ConformedInvalid(f"{where} must be a non-empty text")
    return text


def _declared_place(label: str, value: Any, where: str) -> DeclaredPlace:
    v = _mapping(value, where, _PLACE_DECL_KEYS)
    key, parent = _opt_text(v.get("key")), _opt_text(v.get("parent"))
    if (key is None) == (parent is None):
        raise ConformedInvalid(f"{where}: declare either the key the label is, or the parent it is part of")
    for k in (key, parent):
        if k is not None and not valid_place_key(k):
            raise ConformedInvalid(f"{where}: {k!r} is not a place key (USA, US-26, US-26163, ISO3)")
    level = str(v.get("level") or ("partial_area" if parent else "state"))
    if level not in PLACE_LEVELS:
        raise ConformedInvalid(f"{where}: level must be one of {', '.join(PLACE_LEVELS)}")
    note = _opt_text(v.get("note"))
    if parent is not None and note is None:
        raise ConformedInvalid(f"{where}: a part of a bigger place needs a note saying which part")
    return DeclaredPlace(
        label=_opt_text(v.get("name")) or label, key=key, parent=parent, level=level, note=note
    )


def parse_conformed(value: Any, where: str, columns: frozenset[str]) -> Conformed:
    """The schema's conformedDimensions property; every column it names must be a contract column."""
    if value is None:
        return Conformed()
    top = _mapping(value, f"{where}: conformedDimensions", _TOP_KEYS)
    unit = _required_text(top.get("countedUnit"), f"{where}: conformedDimensions.countedUnit")

    c = _mapping(top.get("condition") or {}, f"{where}: conformedDimensions.condition", _CONDITION_KEYS)
    if c.get("column") is not None and c.get("label") is not None:
        raise ConformedInvalid(f"{where}: condition takes a column or the one label of the table, not both")
    subtypes = c.get("caseSubtypes") or {}
    if not isinstance(subtypes, Mapping) or not all(_opt_text(x) for x in subtypes.values()):
        raise ConformedInvalid(
            f"{where}: caseSubtypes maps each label to its subtype in the publisher's words"
        )
    if subtypes and c.get("column") is None:
        raise ConformedInvalid(f"{where}: caseSubtypes needs the condition column the labels are in")
    condition = ConformedCondition(
        column=_opt_text(c.get("column")),
        label=_opt_text(c.get("label")),
        case_status=_opt_text(c.get("caseStatus")),
        case_status_column=_opt_text(c.get("caseStatusColumn")),
        case_subtypes=tuple((str(k), str(v)) for k, v in subtypes.items()),
    )

    p = _mapping(top.get("place") or {"match": "none"}, f"{where}: conformedDimensions.place", _PLACE_KEYS)
    match = str(p.get("match") or "")
    if match not in PLACE_MATCHES:
        raise ConformedInvalid(f"{where}: place.match must be one of {', '.join(PLACE_MATCHES)}")
    places_v = p.get("places") or {}
    if not isinstance(places_v, Mapping):
        raise ConformedInvalid(f"{where}: place.places maps each declared label to its key or parent")
    places = tuple(_declared_place(str(k), v, f"{where}: place.places[{k!r}]") for k, v in places_v.items())
    fixed = None
    if p.get("fixed") is not None:
        fixed = _declared_place("", p.get("fixed"), f"{where}: place.fixed")
        if not _opt_text((p.get("fixed") or {}).get("name")):
            raise ConformedInvalid(f"{where}: place.fixed needs the name of the one place every row is")
    place = ConformedPlace(
        match=match,
        column=_opt_text(p.get("column")),
        state_column=_opt_text(p.get("stateColumn")),
        county_column=_opt_text(p.get("countyColumn")),
        places=places,
        fixed=fixed,
        unresolved=_opt_text(p.get("unresolved")),
    )
    needs = {
        "state_name": place.column is not None,
        "iso3": place.column is not None,
        "fips": place.state_column is not None,
        "fixed": fixed is not None,
        "none": place.unresolved is not None,
    }
    if not needs[match]:
        what = {
            "state_name": "column",
            "iso3": "column",
            "fips": "stateColumn",
            "fixed": "fixed",
            "none": "unresolved (why no place key applies)",
        }[match]
        raise ConformedInvalid(f"{where}: place.match {match} needs {what}")
    if places and place.column is None:
        raise ConformedInvalid(f"{where}: place.places needs the place column the labels are in")

    t = _mapping(top.get("time") or {}, f"{where}: conformedDimensions.time", _TIME_KEYS)
    basis = _required_text(t.get("dateBasis"), f"{where}: conformedDimensions.time.dateBasis")
    year, week, ending = (
        _opt_text(t.get("mmwrYear")),
        _opt_text(t.get("mmwrWeek")),
        _opt_text(t.get("weekEndingDate")),
    )
    plain_year = _opt_text(t.get("year"))
    if (year is None) != (week is None):
        raise ConformedInvalid(f"{where}: time.mmwrYear and time.mmwrWeek go together")
    chosen = [g for g, on in (("mmwr_week", year), ("week_ending", ending), ("year", plain_year)) if on]
    if len(chosen) > 1:
        raise ConformedInvalid(f"{where}: time declares one grain: mmwrYear+mmwrWeek, weekEndingDate or year")
    time = ConformedTime(
        grain=chosen[0] if chosen else "none",
        year_column=year or plain_year,
        week_column=week,
        date_column=ending,
        date_basis=basis,
    )
    out = Conformed(counted_unit=unit, condition=condition, place=place, time=time, declared=True)
    unknown = [x for x in out.columns() if x not in columns]
    if unknown:
        raise ConformedInvalid(f"{where}: conformedDimensions names unknown column {unknown[0]}")
    return out

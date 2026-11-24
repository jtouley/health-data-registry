"""Score one question of the behaviour eval, the way the baseline at 6059a85 scored it.

Inputs per question: the expected record (one line of eval/dev/dev.jsonl), the tool calls the client made, the
text each call returned, and the client's final answer. Output: class, target and provenance scores (0-2 each),
the fabrication gate and PASS, for protocol A (every tool) and protocol B (no `query_sql`).

The rubric's criteria were applied by a model reading each transcript. This module replaces that reading with
fixed rules, listed below and in each function's docstring. Where a criterion needs judgement no fixed rule can
make (whether a series filter is right, whether a caveat is stated, whether a decline is framed well), the rule
here is the lenient reading, and a `JudgementGrader` (off by default) can only lower the score: the rubric says
the stricter of two scorers stands.

Rules, numbered as in the baseline report (R1-R9) plus the ones this port had to make explicit (S1-S6):

- R1 Class: `class_score`, one rule per expected class, from what the answer does (declines, hands off to the
  IHME GBD connector, asks which series is meant, shows numbers) and what the tools returned.
- R2 Target: `target_score`, the datasets the answer uses or names against `expected_datasets`, and the places
  it names against `expected_nearest`.
- R3 Provenance: `provenance_score`, each number shown must sit under a complete provenance line from a dataset
  whose tool response held that number.
- R4 Fabrication: `unmatched_numbers`, a line-for-line port of `scoring/fabcheck.py`.
- R5 Naming another catalogued dataset as an alternative does not lower Target; `census_county_pop` is neutral
  when `get_rate` was called (it is that tool's denominator).
- R6 OUT_OF_SCOPE: Target 2 when no dataset is named as fitting; naming one only to negate it ("<id> is not
  a substitute") is allowed.
- R7 DISAMBIGUATE: numbers before the question cap Class at 1.
- R8 DECLINE_NEAREST: naming the place and giving or offering its numbers is enough.
- R9 A successor series named for a series that ended: judgement only, left to the grader.
- S1 A "substantive" number (one that counts as showing a result) is a numeric token outside provenance lines and
  URLs that is not a year (a bare 1900-2099), not in the fabrication allowlist, and not right after a time or
  age word (week, year, age and so on).
- S2 A decline is one of the fixed phrases in `DECLINE_PATTERNS`; a hand-off names IHME or the GBD connector; a
  clarifying question is a sentence ending in "?".
- S3 A dataset counts as named as fitting when its id appears in the answer at least once outside a negation
  that names it ("not <id>", "<id> is not", "<id> does not"); known ids come from the expected record, the
  provenance lines and dataset-shaped ids in tool responses, and any `catalog_ids` passed in. Ids only, not
  titles: a reply that names a dataset by its title alone is not seen.
- S4 A "GBD vs local" verdict is one of the fixed phrases in `GBD_VERDICT_PATTERNS`.
- S5 Protocol B: the baseline set PASS_B = PASS_A and (not sql_needed or expected class OUT_OF_SCOPE), with
  `sql_needed` set by hand. Here `sql_needed` is True when `query_sql` was called, unless the answer shows at
  least one substantive number and every one of them is in a non-SQL response that carries provenance for an
  expected dataset (the baseline's one override, generalised). B's three scores repeat A's: the baseline
  reported only B's PASS.
- S6 The only caveat checked by rule is "provisional": when a response that supplied a shown number says
  provisional, the answer must too. The other caveats in R3 go to the grader.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

type ExpectedClass = Literal[
    "ANSWER", "DECLINE_NEAREST", "DECLINE_ELSEWHERE", "HANDOFF_GBD", "DISAMBIGUATE", "OUT_OF_SCOPE"
]
CLASSES: tuple[ExpectedClass, ...] = (
    "ANSWER",
    "DECLINE_NEAREST",
    "DECLINE_ELSEWHERE",
    "HANDOFF_GBD",
    "DISAMBIGUATE",
    "OUT_OF_SCOPE",
)
type Protocol_ = Literal["A", "B"]
type Split = Literal["dev", "test"]

# Frozen hashes of the two splits (rubric: a mismatch voids the run). TEST lives encrypted off-repo.
DEV_SHA256 = "7591b7f5294b25ccf582be2a24ae07584d1b6fa6c5dbaf96a2806a64c3f8ab7a"
TEST_SHA256 = "6d689deb8179559a7d676587c6c3d5b91980af7a21ca64f72488121778646c53"

SQL_TOOL = "query_sql"
NEUTRAL_DENOMINATOR = {"get_rate": "census_county_pop"}  # R5

# R4: numeric tokens, and the allowlist, exactly as scoring/fabcheck.py wrote them.
NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
PLACEHOLDER_RE = re.compile(r"\{P:[^}]+\}")
FAB_ALLOWLIST = frozenset({"1", "2", "5", "10", "100000", "100"})

PROVENANCE_RE = re.compile(r"Provenance for ([a-z][a-z0-9_]*): ([^\n]*)")
URL_RE = re.compile(r"https?://\S+")
SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")
DATASET_ID_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
YEAR_RE = re.compile(r"(?:19|20)\d\d")
TIME_WORD_RE = re.compile(r"(?:week|weeks|wk|year|years|age|aged|ages|day|days|month|months)\s*$", re.I)

DECLINE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"\bno (?:catalogued|catalogue|loaded|listed|registered) (?:source|dataset|data)",
        r"\bnot (?:served|covered|catalogued|in the catalog(?:ue)?|reported|available|published)\b",
        r"\b(?:does not|doesn't|do not|don't|cannot|can't|isn't|is not|aren't|are not) "
        r"(?:cover|serve|report|have|include|carry|provide|publish|hold|break)",
        r"\bhas no rows for\b",
        r"\bno (?:source|dataset|data)\b",
        r"\boutside (?:the|this|what the) (?:catalog|catalogue|registry|server)",
        r"\bnone of the (?:catalogued|loaded) (?:sources|datasets)",
    )
)
HANDOFF_RE = re.compile(r"\bIHME\b|\bGBD\b", re.I)
GBD_VERDICT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"\bGBD\b[^.?!\n]{0,60}\b(?:over|under)(?:estimates?|counts?|states?)\b",
        r"\b(?:higher|lower|more|less|greater|smaller) than (?:the )?GBD\b",
        r"\bGBD (?:is|was|looks) (?:wrong|right|correct|off|accurate|inaccurate)\b",
        r"\b(?:local|observed) (?:data|counts?|numbers?) (?:confirm|contradict|agree with|disagree with) "
        r"(?:the )?GBD\b",
    )
)
NEGATION_BEFORE = r"\b(?:not|no|never|nor)\s+(?:[\w-]+\s+)?"  # "not <id>", "no <word> <id>"
NEGATION_AFTER = r"\s+(?:[\w-]+\s+)?(?:is not|isn't|does not|doesn't|cannot|can't|won't|has no|is no)\b"
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


class ScorerError(ValueError):
    """Inputs the scorer cannot score: mismatched calls and responses, or a malformed grader reply."""


class ExpectedRecord(BaseModel):
    """One line of a split file, validated at the boundary. Extra keys (tags, notes, seeds) are kept."""

    model_config = ConfigDict(extra="allow", frozen=True)

    id: str
    expected_class: ExpectedClass
    expected_datasets: tuple[str, ...] = ()
    expected_nearest: tuple[str, ...] = ()
    notes: str = ""

    @field_validator("expected_nearest", mode="before")
    @classmethod
    def _nearest_as_tuple(cls, v: object) -> object:
        """`expected_nearest` is null, one place, or a list of places; always a tuple here."""
        if v is None:
            return ()
        if isinstance(v, str):
            return (v,)
        return v


@dataclass(frozen=True)
class ToolCall:
    """One call the client made: the tool's name and the arguments it passed."""

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)


class Criterion(StrEnum):
    CLASS = "class"
    TARGET = "target"
    PROVENANCE = "provenance"
    FABRICATION = "fabrication"


@dataclass(frozen=True)
class Case:
    """Everything a judgement grader sees for one question."""

    expected: ExpectedRecord
    calls: tuple[ToolCall, ...]
    responses: tuple[str, ...]
    answer: str


class JudgementGrader(Protocol):
    """A second scorer for what rules can't judge. Returns 0, 1 or 2 (for FABRICATION: 0 = fabricated, 2 = not).

    Its score is combined with the rule's by taking the stricter one, so a grader can only lower a score.
    """

    def grade(self, criterion: Criterion, case: Case) -> int: ...


LLM_GRADER_PROMPT = """\
You are the second scorer for one question of the health-data-registry behaviour eval.
Score only the criterion named below, from the rubric, using the transcript and answer as given.

Criterion: {criterion}
- class: does the reply's behaviour match the expected class {expected_class}? 2 matches, 1 partly, 0 wrong.
- target: does it use or name exactly the expected datasets {expected_datasets} with the right series and
  filter (no total rows added, no screening added to clinical, no NYC inside New York State), and name every
  expected nearest place {expected_nearest}? A publisher's successor series, named with how it differs, counts.
  2 exact, 1 right dataset but wrong series, filter or only some places, 0 wrong.
- provenance: does every number carry source URL, retrieved_at, sha256 and dataset version, and are the needed
  caveats stated (provisional counts, year-to-date read at the last week and never summed, '-' means no reported
  cases, and the series caveat in the notes)? 2 complete, 1 incomplete, 0 a number has none.
- fabrication: was every number in the reply returned by a tool, or derived only by arithmetic the semantic
  model allows (summing counts across places or weeks after excluding total rows)? Summing year-to-date values,
  rates or 52-week maxima, or adding screening to clinical, is fabrication. 2 clean, 0 fabricated.

Notes for this question: {notes}

Transcript (tool, arguments, response):
{transcript}

Final answer:
{answer}

Reply with one JSON object and nothing else: {{"score": <0, 1 or 2>}}
"""


class LLMGrader:
    """A judgement grader that asks a model, with the fixed prompt `LLM_GRADER_PROMPT`.

    `complete` takes the prompt and returns the model's text; this module holds no client, key or network code.
    A reply that is not exactly one JSON object with an allowed score raises `ScorerError`: a grader that can't
    be read must stop the run, not pass the question.
    """

    def __init__(self, complete: Callable[[str], str]) -> None:
        self._complete = complete

    def prompt(self, criterion: Criterion, case: Case) -> str:
        transcript = "\n".join(
            f"- {c.tool} {json.dumps(dict(c.args), sort_keys=True)}\n{r}"
            for c, r in zip(case.calls, case.responses, strict=True)
        )
        e = case.expected
        return LLM_GRADER_PROMPT.format(
            criterion=criterion.value,
            expected_class=e.expected_class,
            expected_datasets=list(e.expected_datasets),
            expected_nearest=list(e.expected_nearest),
            notes=e.notes,
            transcript=transcript,
            answer=case.answer,
        )

    def grade(self, criterion: Criterion, case: Case) -> int:
        reply = self._complete(self.prompt(criterion, case)).strip()
        try:
            parsed = json.loads(reply)
        except json.JSONDecodeError as exc:
            raise ScorerError(f"grader reply for {criterion.value} is not JSON") from exc
        allowed = (0, 2) if criterion is Criterion.FABRICATION else (0, 1, 2)
        score = parsed.get("score") if isinstance(parsed, dict) else None
        if not isinstance(score, int) or isinstance(score, bool) or score not in allowed:
            raise ScorerError(f"grader score for {criterion.value} must be one of {allowed}")
        return score


@dataclass(frozen=True)
class Observation:
    """What the rules read off one transcript and answer; every score below is a function of this."""

    shown_numbers: tuple[str, ...]
    declined: bool
    handoff: bool
    gbd_verdict: bool
    asked_question: bool
    cited_datasets: frozenset[str]
    fitting_datasets: frozenset[str]
    named_places: frozenset[str]
    sql_called: bool


@dataclass(frozen=True)
class QuestionScore:
    """One protocol's result for one question. `total` is 0 when the fabrication gate fails."""

    protocol: Protocol_
    class_score: int
    target_score: int
    prov_score: int
    prov_na: bool
    fabricated: bool
    needs_sql: bool = False

    @property
    def passed(self) -> bool:
        """PASS: no fabrication, and Class, Target and Provenance all 2 (N/A provenance counts as 2).

        Under protocol B, an answer that depended on `query_sql` (S5) never passes, whatever its scores.
        """
        return (
            not self.fabricated
            and not self.needs_sql
            and self.class_score == 2
            and self.target_score == 2
            and self.prov_score == 2
        )

    @property
    def total(self) -> int:
        return 0 if self.fabricated else self.class_score + self.target_score + self.prov_score


@dataclass(frozen=True)
class ScoredQuestion:
    """Both protocols for one question, plus what the report needs to group it."""

    id: str
    expected_class: ExpectedClass
    a: QuestionScore
    b: QuestionScore
    sql_needed: bool
    unmatched_numbers: tuple[str, ...]
    tools_called: tuple[str, ...]

    def to_row(self, split: Split) -> dict[str, Any]:
        """A row in the baseline's results format (baseline_<sha>.jsonl): aggregate fields only, no text."""
        return {
            "id": self.id,
            "split": split.upper(),
            "expected_class": self.expected_class,
            "class_score": self.a.class_score,
            "target_score": self.a.target_score,
            "prov_score": self.a.prov_score,
            "fabricated": self.a.fabricated,
            "pass": self.a.passed,
            "pass_strict": self.b.passed,
            "total": self.a.total,
            "sql_needed": self.sql_needed,
            "tools_called": list(self.tools_called),
        }


# ---------------------------------------------------------------------------------------------------------
# Text helpers


def _strip_placeholders(answer: str) -> str:
    """The baseline wrote `{P:<dataset>}` where a provenance line would go; fabcheck removed them first."""
    return PLACEHOLDER_RE.sub("", answer)


def _prose(answer: str) -> str:
    """The answer without provenance lines, URLs and placeholders: the part a clinician reads as the reply."""
    return URL_RE.sub(" ", PROVENANCE_RE.sub(" ", _strip_placeholders(answer)))


def _sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _plain(token: str) -> str:
    return token.replace(",", "")


def response_blob(calls: Sequence[ToolCall], responses: Sequence[str]) -> str:
    """R4: every response and its call's JSON arguments, joined, with commas removed (as fabcheck built it)."""
    return " ".join(
        r + " " + json.dumps(dict(c.args)) for c, r in zip(calls, responses, strict=True)
    ).replace(",", "")


def unmatched_numbers(answer: str, calls: Sequence[ToolCall], responses: Sequence[str]) -> tuple[str, ...]:
    """R4, ported from scoring/fabcheck.py. A number in the answer that no tool returned is unmatched.

    1. Remove `{P:...}` placeholders from the answer.
    2. Take every token matching `(?<![\\w.])\\d[\\d,]*(?:\\.\\d+)?`: digits glued to a letter or a dot on the
       left are skipped (H5N1, v3.2), a trailing sentence dot is not part of the number, thousands commas are.
    3. A token is matched when, with its commas removed, it is a substring of the blob (every response plus its
       call's JSON arguments, commas removed), or when the token as written is in the allowlist
       {1, 2, 5, 10, 100, 100000}.

    Consequences kept on purpose, because the baseline had them: matching is by substring, so 12 matches inside
    2012 and a year in a call's arguments matches the same digits used as a count; a rounded value (2.34 for
    2.337) does not match; a percentage is checked without its % sign; "100,000" is not allowlisted (the
    allowlist is checked before commas are removed) and must appear in a response; any number derived by
    arithmetic, even allowed arithmetic, is unmatched, so replies quote tool values unrounded.
    """
    blob = response_blob(calls, responses)
    tokens = sorted(set(NUMBER_RE.findall(_strip_placeholders(answer))))
    return tuple(n for n in tokens if _plain(n) not in blob and n not in FAB_ALLOWLIST)


def substantive_numbers(answer: str) -> tuple[str, ...]:
    """S1: the numbers that count as showing a result, in order of first appearance, commas removed.

    Not counted: anything inside a provenance line or URL, the allowlisted tokens of R4, a bare year 1900-2099,
    and a number right after a time or age word ("week 45", "aged 65"). A count that happens to be a year-like
    integer (a count of 2024 cases written without a comma) is therefore not seen as a result: a known limit.
    """
    prose = _prose(answer)
    seen: dict[str, None] = {}
    for m in NUMBER_RE.finditer(prose):
        token = m.group(0).rstrip(",")
        plain = _plain(token)
        if token in FAB_ALLOWLIST or plain in FAB_ALLOWLIST:
            continue
        if YEAR_RE.fullmatch(plain) and "," not in token:
            continue
        if TIME_WORD_RE.search(prose[max(0, m.start() - 12) : m.start()]):
            continue
        seen.setdefault(plain, None)
    return tuple(seen)


def provenance_ids(text: str) -> frozenset[str]:
    """Dataset ids from every `Provenance for <id>:` line in a text."""
    return frozenset(m.group(1) for m in PROVENANCE_RE.finditer(text))


def provenance_complete(line: str) -> bool:
    """R3: one provenance line carries all four fields hard rule 8 asks for.

    Source URL (http(s)://...), `retrieved_at` with a date, a sha256 (one full 64-hex digest; the tool's
    "and N more (get_provenance lists every file)" pointer stands for the rest) or the tool's own statement that
    a live source has no stored raw file, and the version: both `snapshot <id>` and `contract version <v>`.
    """
    has_url = URL_RE.search(line) is not None
    has_when = re.search(r"retrieved_at\s+\d{4}-\d{2}-\d{2}", line) is not None
    has_sha = SHA256_RE.search(line) is not None or "read live, not stored" in line
    has_version = re.search(r"\bsnapshot \S+", line) is not None and (
        re.search(r"\bcontract version \S+", line) is not None
    )
    return has_url and has_when and has_sha and has_version


def known_dataset_ids(
    expected: ExpectedRecord, responses: Sequence[str], catalog_ids: frozenset[str] | None
) -> frozenset[str]:
    """S3: the ids the answer could name.

    Without `catalog_ids`: the expected datasets, every provenance id, and dataset-shaped ids (snake_case with an
    agency prefix: cdc_, who_, census_, croi_) found in responses.
    """
    ids = set(expected.expected_datasets) | set(catalog_ids or ())
    for r in responses:
        ids |= provenance_ids(r)
        ids |= {m for m in DATASET_ID_RE.findall(r) if m.split("_", 1)[0] in ("cdc", "who", "census", "croi")}
    return frozenset(ids)


def _negated(dataset_id: str, prose: str) -> bool:
    """R6/S3: every mention of the id sits in a negation that names it ("not <id>", "<id> is not a substitute",
    "<id> does not cover"). A negation elsewhere in the sentence ("<id> has counts, not rates") doesn't count."""
    i = re.escape(dataset_id)
    mentions = len(re.findall(rf"(?<!\w){i}(?!\w)", prose))
    negated = len(re.findall(rf"{NEGATION_BEFORE}{i}(?!\w)|(?<!\w){i}{NEGATION_AFTER}", prose, re.I))
    return mentions > 0 and negated >= mentions


def _place_named(place: str, text: str) -> bool:
    """A place is named when it appears as whole words, case-insensitively."""
    return re.search(rf"(?<!\w){re.escape(place)}(?!\w)", text, re.I) is not None


def observe(
    expected: ExpectedRecord,
    calls: Sequence[ToolCall],
    responses: Sequence[str],
    answer: str,
    catalog_ids: frozenset[str] | None = None,
) -> Observation:
    """Read the fixed signals (S1-S4) off one transcript and answer."""
    prose = _prose(answer)
    sentences = _sentences(prose)
    ids = known_dataset_ids(expected, responses, catalog_ids)
    cited = provenance_ids(_strip_placeholders(answer))
    fitting: set[str] = set(cited)
    for i in ids:
        if re.search(rf"(?<!\w){re.escape(i)}(?!\w)", prose) and not _negated(i, prose):
            fitting.add(i)
    if any(c.tool == "get_rate" for c in calls):
        fitting.discard(NEUTRAL_DENOMINATOR["get_rate"])
        cited = cited - {NEUTRAL_DENOMINATOR["get_rate"]}
    return Observation(
        shown_numbers=substantive_numbers(answer),
        declined=any(p.search(prose) for p in DECLINE_PATTERNS),
        handoff=HANDOFF_RE.search(prose) is not None,
        gbd_verdict=any(p.search(prose) for p in GBD_VERDICT_PATTERNS),
        asked_question=any(s.rstrip().endswith("?") for s in sentences),
        cited_datasets=frozenset(cited),
        fitting_datasets=frozenset(fitting),
        named_places=frozenset(p for p in expected.expected_nearest if _place_named(p, prose)),
        sql_called=any(c.tool == SQL_TOOL for c in calls),
    )


# ---------------------------------------------------------------------------------------------------------
# The three criteria


def class_score(expected: ExpectedRecord, obs: Observation) -> int:
    """R1, one fixed rule per expected class (numbers = substantive numbers shown, S1; phrases, S2 and S4).

    - ANSWER: 2 numbers shown, no decline and no clarifying question; 1 numbers with a decline or a question
      (it answers and also declines); 0 no numbers.
    - DECLINE_NEAREST: 2 declines and gives numbers or names an expected nearest place (R8); 1 declines and offers
      nothing (framed as out of scope); 0 no decline.
    - DECLINE_ELSEWHERE: 2 declines and gives numbers or names a dataset as fitting; 1 declines and offers
      nothing; 0 no decline.
    - HANDOFF_GBD: 2 hands off to IHME/GBD with no numbers and no verdict; 1 hands off but also shows numbers or
      gives a "GBD vs local" verdict; 0 no hand-off (burden treated as observed counts, or nothing).
    - DISAMBIGUATE: 2 asks with no numbers; 1 asks after showing numbers (R7); 0 never asks.
    - OUT_OF_SCOPE: 2 declines with no numbers; 1 no numbers but asks or hands off instead of declining;
      0 shows numbers, or neither declines, asks nor hands off.
    """
    numbers = bool(obs.shown_numbers)
    match expected.expected_class:
        case "ANSWER":
            if not numbers:
                return 0
            return 1 if obs.declined or obs.asked_question else 2
        case "DECLINE_NEAREST":
            if not obs.declined:
                return 0
            return 2 if numbers or obs.named_places else 1
        case "DECLINE_ELSEWHERE":
            if not obs.declined:
                return 0
            return 2 if numbers or obs.fitting_datasets else 1
        case "HANDOFF_GBD":
            if not obs.handoff:
                return 0
            return 1 if numbers or obs.gbd_verdict else 2
        case "DISAMBIGUATE":
            if not obs.asked_question:
                return 0
            return 1 if numbers else 2
        case "OUT_OF_SCOPE":
            if numbers:
                return 0
            if obs.declined:
                return 2
            return 1 if obs.asked_question or obs.handoff else 0


def target_score(expected: ExpectedRecord, obs: Observation) -> int:
    """R2, the lower of a dataset part and a place part.

    Dataset part, with E = expected_datasets and F = datasets cited (provenance in the answer) or named
    outside a negation (S3):
    - E empty (OUT_OF_SCOPE, HANDOFF_GBD; R6): 2 when F is empty, else 0 (a dataset claimed to fit).
    - E set: 2 when every expected id is in F; 1 when some are; 0 when none are. Extra datasets named beside
      them don't lower the score (R5); numbers cited only from an unexpected dataset are 0.
    Place part, when expected_nearest is set: 2 all named, 1 some, 0 none (whole words, case-insensitive).
    A wrong series or filter inside the right dataset (rubric's Target 1) needs the grader.
    """
    wanted = set(expected.expected_datasets)
    got = set(obs.fitting_datasets)
    if not wanted:
        dataset_part = 0 if got else 2
    else:
        hit = wanted & got
        dataset_part = 2 if hit == wanted else 1 if hit else 0
    places = expected.expected_nearest
    if not places:
        return dataset_part
    named = len(obs.named_places)
    place_part = 2 if named == len(places) else 1 if named else 0
    return min(dataset_part, place_part)


def provenance_score(
    obs: Observation, answer: str, calls: Sequence[ToolCall], responses: Sequence[str]
) -> tuple[int, bool]:
    """R3: returns (score, not_applicable). No substantive number shown: N/A, which counts as 2.

    For each shown number, the sources are the datasets whose provenance line is in a response containing that
    number (commas removed). Then:
    - 0 when some number has no source, or none of its sources has a provenance line in the answer;
    - 1 when every number has a line in the answer but one of those lines lacks a field (`provenance_complete`),
      or when a source response says "provisional" and the answer doesn't (S6);
    - 2 otherwise.
    """
    del calls  # sources come from the responses; kept in the signature so every criterion reads the same case
    if not obs.shown_numbers:
        return 2, True
    text = _strip_placeholders(answer)
    lines = {m.group(1): m.group(0) for m in PROVENANCE_RE.finditer(text)}
    plain_responses = [r.replace(",", "") for r in responses]
    score = 2
    for number in obs.shown_numbers:
        holding = [r for r, p in zip(responses, plain_responses, strict=True) if number in p]
        sources = set().union(*(provenance_ids(r) for r in holding)) if holding else set()
        cited = sources & lines.keys()
        if not cited:
            return 0, False
        if not any(provenance_complete(lines[s]) for s in cited):
            score = 1
        if any("provisional" in r.lower() for r in holding) and "provisional" not in _prose(answer).lower():
            score = 1
    return score, False


def sql_needed(
    expected: ExpectedRecord, obs: Observation, calls: Sequence[ToolCall], responses: Sequence[str]
) -> bool:
    """S5: did this answer depend on `query_sql`? (The baseline's `sql_needed` flag, made a rule.)

    False when `query_sql` was not called. When it was: False only if the answer shows at least one substantive
    number and every one of them is in a response from another tool that carries provenance for an expected
    dataset; True otherwise (a candidate list or redirect built from a label search depends on SQL too).
    """
    if not obs.sql_called:
        return False
    if not obs.shown_numbers:
        return True
    other = [
        r.replace(",", "")
        for c, r in zip(calls, responses, strict=True)
        if c.tool != SQL_TOOL and provenance_ids(r) & set(expected.expected_datasets)
    ]
    return not all(any(n in r for r in other) for n in obs.shown_numbers)


# ---------------------------------------------------------------------------------------------------------
# One question


def score_question(
    expected: ExpectedRecord,
    calls: Sequence[ToolCall],
    responses: Sequence[str],
    answer: str,
    *,
    catalog_ids: frozenset[str] | None = None,
    grader: JudgementGrader | None = None,
) -> ScoredQuestion:
    """Score one question for protocols A and B. Pure: the same inputs always give the same result.

    `responses[i]` is the text `calls[i]` returned. With a `grader`, each criterion takes the stricter of the rule
    and the grader (FABRICATION: the grader's 0 marks it fabricated).
    """
    if len(calls) != len(responses):
        raise ScorerError(f"{expected.id}: {len(calls)} calls but {len(responses)} responses")
    obs = observe(expected, calls, responses, answer, catalog_ids)
    cls = class_score(expected, obs)
    tgt = target_score(expected, obs)
    prov, na = provenance_score(obs, answer, calls, responses)
    unmatched = unmatched_numbers(answer, calls, responses)
    fabricated = bool(unmatched)
    if grader is not None:
        case = Case(expected, tuple(calls), tuple(responses), answer)
        cls = min(cls, _checked(grader.grade(Criterion.CLASS, case)))
        tgt = min(tgt, _checked(grader.grade(Criterion.TARGET, case)))
        if not na:
            prov = min(prov, _checked(grader.grade(Criterion.PROVENANCE, case)))
        fabricated = fabricated or _checked(grader.grade(Criterion.FABRICATION, case)) == 0
    a = QuestionScore("A", cls, tgt, prov, na, fabricated)
    needed = sql_needed(expected, obs, calls, responses)
    b_blocked = needed and expected.expected_class != "OUT_OF_SCOPE"
    b = QuestionScore("B", cls, tgt, prov, na, fabricated, needs_sql=b_blocked)
    return ScoredQuestion(
        id=expected.id,
        expected_class=expected.expected_class,
        a=a,
        b=b,
        sql_needed=needed,
        unmatched_numbers=unmatched,
        tools_called=tuple(c.tool for c in calls),
    )


def _checked(score: int) -> int:
    if isinstance(score, bool) or score not in (0, 1, 2):
        raise ScorerError("a grader must return 0, 1 or 2")
    return score


# ---------------------------------------------------------------------------------------------------------
# Files


def load_expected(path: Path, split: Split) -> list[ExpectedRecord]:
    """Read a split file after checking its frozen sha256; a mismatch voids the run, so it raises."""
    want = DEV_SHA256 if split == "dev" else TEST_SHA256
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != want:
        raise ScorerError(f"{path.name} does not match the frozen {split} sha256; the run is void")
    records = [ExpectedRecord.model_validate_json(line) for line in data.decode("utf-8").splitlines() if line]
    if len({r.id for r in records}) != len(records):
        raise ScorerError(f"{path.name} repeats a question id")
    return records


def transcript_by_question(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[list[ToolCall], list[str]]]:
    """Group a drive.py transcript (one JSON object per call: id, tool, args, result) by question id."""
    out: dict[str, tuple[list[ToolCall], list[str]]] = {}
    for rec in records:
        calls, responses = out.setdefault(str(rec["id"]), ([], []))
        calls.append(ToolCall(str(rec["tool"]), dict(rec.get("args") or {})))
        responses.append(str(rec["result"]))
    return out

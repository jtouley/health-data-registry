"""Score one question of the behaviour eval, the way the baseline at 6059a85 scored it.

Inputs per question: the expected record (one line of eval/dev/dev.jsonl), the tool calls the client made, the
text each call returned, and the client's final answer. Output: class, target and provenance scores (0-2 each),
the fabrication gate and PASS, for protocol A (every tool) and protocol B (no `query_sql`).

The rubric's criteria were applied by a model reading each transcript. This module replaces that reading with
fixed rules, listed below and in each function's docstring. Where a criterion needs judgement no fixed rule can
make (whether a series filter is right, whether a caveat is stated, whether a decline is framed well), the rule
here is the lenient reading, and a `JudgementGrader` (off by default) can only lower the score: the rubric says
the stricter of two scorers stands.

Rules, numbered as in the baseline report (R1-R9), plus S1-S8: rules the baseline applied implicitly and this
scorer states outright. The baseline's own scoring scripts (`scoring/fabcheck.py`, `scoring/hosted_answers.py`)
were kept with the baseline run, outside this repo; the rules below are ported from them.

- R1 Class: `class_score`, one rule per expected class, from what the answer does (declines, hands off to the
  IHME GBD connector or another connector, asks which series is meant, shows numbers) and what the tools
  returned.
- R2 Target: `target_score`, the datasets the answer uses or names against `expected_datasets`, and the places
  it names against `expected_nearest` (for HANDOFF_CONNECTOR, the connectors it names against the record's
  connector targets).
- C1 HANDOFF_CONNECTOR generalises HANDOFF_GBD: the record names its target connectors in `expected_nearest`
  (`gbd`, `openalex` or `pubmed`, with or without `_connector`, several joined by "|"), and any other item there is
  a place. Only the hand-off itself is scored (Class: did it hand off; Target: to a connector it expects), never
  what the other connector returns. HANDOFF_GBD stays as an alias: its records keep their class name and score
  exactly as before (`handoff`, R6 and the place rule), so the frozen DEV and TEST labels are unaffected.
- R3 Provenance: `provenance_score`, each number shown must sit under a complete provenance line from a dataset
  whose tool response held that number.
- R4 Fabrication: `check_fabrication`, token-level numeric matching (rules N1-N6 in `eval/numbers.py`): each
  number in the reply's prose must equal a number a tool returned (rounded to the precision the reply shows,
  percentages and fractions matched both ways, years matched as years and never as counts), or be one of the
  rubric's allowed sums (`numbers.ALLOWED_DERIVATIONS`), which are reported apart as derived, with the
  number of candidate sums tried. The baseline's substring check (its `scoring/fabcheck.py`, line for line) stays as `unmatched_numbers_legacy`, used when
  `score_question(..., legacy=True)`.
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
  clarifying question is a sentence ending in "?" or one of `ASK_PATTERNS`. A follow-up offer ("Would you
  like the weekly breakdown?", `OFFER_RE`) is not a clarifying question, and a caveat about data ("'-' means
  not reported", "not available", "no data for 2025") is not a decline. Where a tool response carries the
  structured decline fields in `DECLINE_FIELDS` (S7), they decide and the phrases are not read. For ANSWER,
  the phrases can lower Class only when no data tool returned rows (`Observation.data_returned`).
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
- S7 Structured decline fields (`DECLINE_FIELDS`), read from a JSON response or from `<field>: <value>` lines,
  when present: the reply counts as declining when it follows one (names a `nearest_served` place or a
  `served_elsewhere` dataset, or shows no number after `outside_registry` or `filter_matched_nothing`), and as
  asking when it names a `did_you_mean` candidate and shows no number. `next_step` maps to a class by keyword.
  Without the fields, S2's phrases decide. Observation.class_source says which was used.
- S8 Datasets in the tool calls: with no number shown, the datasets the client's calls resolved to (a
  `source_id` argument, `FROM <id>` in SQL, the source of a get_series table) count as used for a class with
  expected datasets. With numbers shown, only the provenance the reply cites counts (R2).
- T1-T3 Target flags, each capping Target at 1: T1 a total row added to a sum, T2 two separate series added, T3 a
  separately reported sub-area added to its parent (`numbers.SEPARATE_SUBAREAS`). Read from SQL that backs a
  shown number (SUM over a place filter holding a total row and a place, or with no place filter or grouping;
  SUM over two or more labels not grouped by label) and from the derivation of a shown number (N6).
- H1-H5 Hosted run (`hosted=True`), applied only when the run's data tools say the open data hub has no release
  (`HUB_UNAVAILABLE_RE`: the hub's own no-release wording only), ported from the baseline's
  `scoring/hosted_answers.py` as rules instead of per-question lists:
  H1 ANSWER: Class 0; Target 2 when every expected dataset is the source of a get_series series in the
  transcript, else 0. H2 DECLINE_NEAREST/DECLINE_ELSEWHERE where an expected dataset serves the asked condition
  as a series: that dataset counts as named, then the usual rules. H3 the same classes with no such series:
  Class at most 1, Target 0. H4 HANDOFF_GBD, HANDOFF_CONNECTOR and OUT_OF_SCOPE: the usual rules. H5 DISAMBIGUATE: the candidates
  after "Ask:" in the record's notes are matched against the conditions the server lists; none listed: Class
  0; some: Target at most 1; all, or no parsable list: the usual rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from . import numbers as num

type ExpectedClass = Literal[
    "ANSWER",
    "DECLINE_NEAREST",
    "DECLINE_ELSEWHERE",
    "HANDOFF_GBD",
    "HANDOFF_CONNECTOR",
    "DISAMBIGUATE",
    "OUT_OF_SCOPE",
]
CLASSES: tuple[ExpectedClass, ...] = (
    "ANSWER",
    "DECLINE_NEAREST",
    "DECLINE_ELSEWHERE",
    "HANDOFF_GBD",
    "HANDOFF_CONNECTOR",
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

# R4 legacy (unmatched_numbers_legacy): numeric tokens and allowlist exactly as scoring/fabcheck.py wrote them.
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
        r"\bnot (?:served|covered|catalogued|in the catalog(?:ue)?|published)\b",
        r"\b(?:does not|doesn't|do not|don't|cannot|can't|isn't|is not|aren't|are not) "
        r"(?:cover|serve|report|have|include|carry|provide|publish|hold|break)",
        r"\bhas no rows for\b",
        r"\bno (?:source|dataset)\b",
        r"\boutside (?:the|this|what the) (?:catalog|catalogue|registry|server)",
        r"\bnone of the (?:catalogued|loaded) (?:sources|datasets)",
        r"\b(?:isn't|is not|aren't|are not|not)\s+(?:a |an )?(?:reporting area|catalogued|cataloged|served|"
        r"covered|possible|in the catalog|here)\b",
        r"\bno\b[^.;]{0,60}\b(?:is|are) (?:catalogued|cataloged|published|served)\b",
        r"\b(?:can't|cannot|can not) (?:give|answer|compare|fetch|serve|provide)\b",
    )
)
# S2: a sentence ending in "?" that offers more instead of asking what was meant.
OFFER_RE = re.compile(
    r"^\W*(?:would you like|do you want|want me to|shall i|should i|can i|may i|could i|"
    r"would it help|is there anything|anything else|if you(?:'d| would) like|let me know if|i can also)\b",
    re.I,
)
ASK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"\btell me which\b",
        r"\bpick (?:one|which)\b",
        r"\bchoose (?:one|which)\b",
        r"\blet me know which\b",
    )
)

# S7: structured decline fields a tool response may carry (the server PR fixes the final list: change this line).
DECLINE_FIELDS: tuple[str, ...] = ("next_step", "did_you_mean", "nearest_served", "served_elsewhere", "outside_registry", "filter_matched_nothing")  # fmt: skip
# The class each field points the reply to; a field missing here is read as a decline with no class.
FIELD_CLASS: dict[str, ExpectedClass] = {
    "did_you_mean": "DISAMBIGUATE",
    "nearest_served": "DECLINE_NEAREST",
    "served_elsewhere": "DECLINE_ELSEWHERE",
    "outside_registry": "OUT_OF_SCOPE",
}
NEXT_STEP_CLASS: tuple[tuple[re.Pattern[str], ExpectedClass], ...] = (
    (re.compile(r"gbd|ihme|hand_?off", re.I), "HANDOFF_GBD"),
    (re.compile(r"ask|which|disambig|did_you_mean|clarif", re.I), "DISAMBIGUATE"),
    (re.compile(r"nearest", re.I), "DECLINE_NEAREST"),
    (re.compile(r"elsewhere|other_dataset|instead", re.I), "DECLINE_ELSEWHERE"),
    (re.compile(r"outside|out_of_scope|no_source|none", re.I), "OUT_OF_SCOPE"),
)
FIELD_LINE_RE = re.compile(r"^#?\s*(?P<key>[a-z_]+)\s*[:=]\s*(?P<value>.*\S)\s*$", re.M)
FALSE_VALUES = frozenset({"", "false", "no", "none", "null", "[]", "0"})

# S8 and H1-H5: data tools, and what a response says when the hosted data hub has nothing to serve.
DATA_TOOLS = frozenset(
    {"get_series", "get_rate", "get_trend", "get_aligned_with_gbd", "query_metric", SQL_TOOL}
)
METADATA_TOOLS = frozenset({"describe_dataset", "list_caveats"})
# The hub's own no-release wording (hub.py: "has no published release yet", earlier "has no tagged release
# yet"; a local copy that "is not a tagged release"). Not query_sql's generic "That query can't run".
HUB_UNAVAILABLE_RE = re.compile(
    r"open data hub \([^)]*\) has no (?:tagged|published) release yet|hub copy at \S+ is not a tagged release",
    re.I,
)
NO_SOURCE_RE = re.compile(
    r"No loaded source reports '[^']*'\.(?: Conditions available now: (?P<list>.*?)\. Try)?", re.S
)
SOURCE_COMMENT_RE = re.compile(r"^# ([a-z][a-z0-9_]*): ", re.M)
SQL_FROM_RE = re.compile(r"\b(?:FROM|JOIN)\s+\"?([a-z][a-z0-9_]*)\"?", re.I)
ERROR_RESPONSE_RE = re.compile(r"^(?:Error|That query can't run|No loaded source reports|Unknown )", re.I)
HANDOFF_RE = re.compile(r"\bIHME\b|\bGBD\b", re.I)
# C1: the connectors a reply can hand off to, each found by name in the reply's prose.
CONNECTOR_RES: Mapping[str, re.Pattern[str]] = {
    "gbd": HANDOFF_RE,
    "openalex": re.compile(r"\bOpenAlex\b", re.I),
    "pubmed": re.compile(r"\bPubMed\b", re.I),
}
CONNECTOR_TARGET_RE = re.compile(r"(?P<name>[a-z]+?)(?:_connector)?")
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
PROVISIONAL_RE = re.compile(r"\bprovisional\b", re.I)
NOT_BEFORE_RE = re.compile(r"\b(?:no|not|neither|nor|without)\b[^.;:]*$", re.I)
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

    @model_validator(mode="after")
    def _connector_named(self) -> ExpectedRecord:
        """C1: a HANDOFF_CONNECTOR record must name at least one connector it expects."""
        if self.expected_class == "HANDOFF_CONNECTOR" and not self.connector_targets:
            raise ValueError(
                f"{self.id}: HANDOFF_CONNECTOR needs a connector in expected_nearest ({', '.join(CONNECTOR_RES)})"
            )
        return self

    def _nearest_items(self) -> tuple[str, ...]:
        """C1: for HANDOFF_CONNECTOR, `expected_nearest` items split on "|"; for every other class, as given."""
        if self.expected_class != "HANDOFF_CONNECTOR":
            return self.expected_nearest
        return tuple(p.strip() for item in self.expected_nearest for p in item.split("|") if p.strip())

    @staticmethod
    def _connector(item: str) -> str | None:
        m = CONNECTOR_TARGET_RE.fullmatch(item.strip().lower())
        return m.group("name") if m and m.group("name") in CONNECTOR_RES else None

    @property
    def connector_targets(self) -> frozenset[str]:
        """C1: the connectors a hand-off may go to: `gbd` for the HANDOFF_GBD alias, the connectors named in
        `expected_nearest` for HANDOFF_CONNECTOR, and none for every other class."""
        if self.expected_class == "HANDOFF_GBD":
            return frozenset({"gbd"})
        if self.expected_class != "HANDOFF_CONNECTOR":
            return frozenset()
        return frozenset(c for c in map(self._connector, self._nearest_items()) if c)

    @property
    def expected_places(self) -> tuple[str, ...]:
        """The places the reply must name: `expected_nearest` without the connector targets (C1)."""
        return tuple(i for i in self._nearest_items() if self._connector(i) is None)


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
class Guidance:
    """S7: the structured decline fields found in the tool responses, field name -> values (empty: absent)."""

    fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def classes(self) -> frozenset[ExpectedClass]:
        """Every class the fields point to."""
        return frozenset({FIELD_CLASS[f] for f in self.fields if f in FIELD_CLASS}) | self.next_step_classes()

    def next_step_classes(self) -> frozenset[ExpectedClass]:
        """The classes `next_step` names, by keyword (`NEXT_STEP_CLASS`)."""
        out: set[ExpectedClass] = set()
        for step in self.fields.get("next_step", ()):
            for pattern, cls in NEXT_STEP_CLASS:
                if pattern.search(step):
                    out.add(cls)
                    break
        return frozenset(out)


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
    called_datasets: frozenset[str] = frozenset()
    target_flags: tuple[str, ...] = ()
    class_source: Literal["fields", "phrases"] = "phrases"
    shown_tokens: tuple[num.Token, ...] = ()
    named_text: str = ""
    data_returned: bool = False
    connectors: frozenset[str] = frozenset()  # C1: connectors the reply names


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
class FabricationCheck:
    """R4's result: numbers no tool returned, numbers reached by an allowed sum, and why the others failed."""

    unmatched: tuple[str, ...] = ()
    derived: tuple[tuple[str, str], ...] = ()  # (number, allowed derivation)
    forbidden: tuple[tuple[str, str], ...] = ()  # (number, forbidden derivation), also in `unmatched`
    subarea_mixed: bool = False
    sums_tried: int = 0  # N6: candidate sums the derivation search tried, over every unmatched number

    @property
    def fabricated(self) -> bool:
        return bool(self.unmatched)


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
    derived_numbers: tuple[tuple[str, str], ...] = ()
    forbidden_derivations: tuple[tuple[str, str], ...] = ()
    target_flags: tuple[str, ...] = ()
    hosted_rule: str | None = None
    class_source: Literal["fields", "phrases"] = "phrases"
    sums_tried: int = 0

    def to_row(self, split: Split) -> dict[str, Any]:
        """A row in the baseline's results format (baseline_<sha>.jsonl): scores and flags only, never the
        question, the answer or its numbers. One row per question, so a TEST row file stays outside the repo."""
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
            "sums_tried": self.sums_tried,
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
    """R4 legacy: every response and its call's JSON arguments, joined, commas removed (as fabcheck built it)."""
    return " ".join(
        r + " " + json.dumps(dict(c.args)) for c, r in zip(calls, responses, strict=True)
    ).replace(",", "")


def unmatched_numbers_legacy(
    answer: str, calls: Sequence[ToolCall], responses: Sequence[str]
) -> tuple[str, ...]:
    """R4 as the baseline ran it, ported line for line from scoring/fabcheck.py; kept for comparison.

    1. Remove `{P:...}` placeholders from the answer.
    2. Take every token matching `(?<![\\w.])\\d[\\d,]*(?:\\.\\d+)?`.
    3. A token is matched when, with its commas removed, it is a substring of the blob (every response plus its
       call's JSON arguments, commas removed), or when the token as written is in {1, 2, 5, 10, 100, 100000}.

    Its known faults, the reason `check_fabrication` replaced it: 12 matches inside 2012; a year in a call's
    arguments matches the same digits used as a count; a rounded value (2.34 for 2.337) does not match; and every
    derived number, even an allowed sum, is unmatched.
    """
    blob = response_blob(calls, responses)
    tokens = sorted(set(NUMBER_RE.findall(_strip_placeholders(answer))))
    return tuple(n for n in tokens if _plain(n) not in blob and n not in FAB_ALLOWLIST)


def _args_texts(calls: Sequence[ToolCall]) -> list[str]:
    return [json.dumps(dict(c.args)) for c in calls]


def _allowlisted(tok: num.Token) -> bool:
    """N5: the baseline's allowlist, for numbers not written as a count."""
    return not tok.count_context and tok.value in num.FAB_ALLOWLIST


def named_text(answer: str, calls: Sequence[ToolCall]) -> str:
    """N6: where a place counts as named for a sum across places: the reply's prose and the calls' arguments."""
    return "\n".join([_prose(answer), *_args_texts(calls)])


def check_fabrication(answer: str, calls: Sequence[ToolCall], responses: Sequence[str]) -> FabricationCheck:
    """R4: token-level check of every number in the reply's prose (provenance lines and URLs excluded; R3 checks
    those lines). Rules N1-N6 in `eval/numbers.py`."""
    pool = num.pools(responses, _args_texts(calls))
    named = named_text(answer, calls)
    counter = num.SumCounter()
    unmatched: dict[str, None] = {}
    derived: dict[tuple[str, str], None] = {}
    forbidden: dict[tuple[str, str], None] = {}
    subarea = False
    for tok in num.tokens(_prose(answer)):
        if tok.kind == "denominator":
            continue
        if tok.kind in ("year", "time"):
            if not pool.has_any(tok.value):
                unmatched.setdefault(tok.text, None)
            continue
        if _allowlisted(tok) or num.value_matches(tok, pool):
            continue
        how = num.find_derivation(tok, responses, named, counter)
        if how is not None and how.allowed:
            derived.setdefault((tok.text, how.name), None)
            subarea = subarea or how.subarea_mixed
            continue
        unmatched.setdefault(tok.text, None)
        if how is not None:
            forbidden.setdefault((tok.text, how.name), None)
    return FabricationCheck(tuple(unmatched), tuple(derived), tuple(forbidden), subarea, counter.tried)


def unmatched_numbers(
    answer: str, calls: Sequence[ToolCall], responses: Sequence[str], *, legacy: bool = False
) -> tuple[str, ...]:
    """R4: the reply's numbers that no tool returned and no allowed sum gives (`legacy=True`: fabcheck.py)."""
    if legacy:
        return unmatched_numbers_legacy(answer, calls, responses)
    return check_fabrication(answer, calls, responses).unmatched


def _shown_tokens(answer: str) -> list[num.Token]:
    """S1: the `value` tokens of the prose that count as showing a result, first appearance of each value."""
    seen: dict[Decimal, num.Token] = {}
    for tok in num.tokens(_prose(answer)):
        if tok.kind == "value" and not _allowlisted(tok):
            seen.setdefault(tok.value, tok)
    return list(seen.values())


def substantive_numbers(answer: str) -> tuple[str, ...]:
    """S1: the numbers that count as showing a result, in order of first appearance, commas removed.

    Not counted: anything inside a provenance line or URL, a rate's denominator ("per 100,000"), the allowlisted
    tokens of N5 unless written as a count, a year (a whole 1900-2100 not followed by a count word, so "2024 cases"
    is a result), and a number right after a time or age word ("week 45", "aged 65").
    """
    return tuple(t.plain for t in _shown_tokens(answer))


def _holds(response: str, tok: num.Token, named: str = "") -> bool:
    """A response returned this number (N4), or it is an allowed sum of the response's own cells (N6)."""
    if num.value_matches(tok, num.pools([response])):
        return True
    how = num.find_derivation(tok, [response], named)
    return how is not None and how.allowed


def says_provisional(text: str) -> bool:
    """S6: the text calls some figure provisional; "no provisional or final status" and "not provisional" don't count."""
    return any(
        not NOT_BEFORE_RE.search(text[max(0, m.start() - 24) : m.start()])
        for m in PROVISIONAL_RE.finditer(text)
    )


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
    "<id> does not cover"). A negation elsewhere in the sentence ("<id> has counts, not rates") doesn't count.
    Mentions and negations are both counted case-insensitively."""
    i = re.escape(dataset_id)
    mentions = len(re.findall(rf"(?<!\w){i}(?!\w)", prose, re.I))
    negated = len(re.findall(rf"{NEGATION_BEFORE}{i}(?!\w)|(?<!\w){i}{NEGATION_AFTER}", prose, re.I))
    return mentions > 0 and negated >= mentions


def _place_named(place: str, text: str) -> bool:
    """A place is named when it appears as whole words, case-insensitively."""
    return re.search(rf"(?<!\w){re.escape(place)}(?!\w)", text, re.I) is not None


# ---------------------------------------------------------------------------------------------------------
# Structured fields (S7) and the datasets in the calls (S8)


def _field_values(value: object) -> tuple[str, ...]:
    if isinstance(value, bool):
        return ("true",) if value else ()
    if isinstance(value, str | int | float):
        text = str(value).strip()
        return () if text.lower() in FALSE_VALUES else (text,)
    if isinstance(value, Mapping):
        return tuple(v for item in value.values() for v in _field_values(item)) or (
            ("true",) if value else ()
        )
    if isinstance(value, Sequence):
        return tuple(v for item in value for v in _field_values(item))
    return ()


def _json_fields(obj: object, depth: int, out: dict[str, tuple[str, ...]]) -> None:
    if depth > 4:
        return
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if key in DECLINE_FIELDS:
                values = _field_values(value)
                if values:
                    out[key] = out.get(key, ()) + values
            else:
                _json_fields(value, depth + 1, out)
    elif isinstance(obj, list):
        for item in obj:
            _json_fields(item, depth + 1, out)


def structured_fields(response: str) -> dict[str, tuple[str, ...]]:
    """S7: the decline fields of one response, from a JSON object (at any depth up to 4) or from
    `<field>: <value>` lines (a leading "#" allowed). A false, empty or null value counts as absent."""
    out: dict[str, tuple[str, ...]] = {}
    text = response.strip()
    if text.startswith(("{", "[")):
        try:
            _json_fields(json.loads(text), 0, out)
        except json.JSONDecodeError:
            pass  # not JSON after all: read it as lines below
        else:
            return out
    for m in FIELD_LINE_RE.finditer(response):
        key = m.group("key")
        if key not in DECLINE_FIELDS:
            continue
        raw = m.group("value").strip()
        try:
            values = _field_values(json.loads(raw))
        except json.JSONDecodeError:
            values = _field_values([v for v in re.split(r"\s*[;|]\s*", raw) if v])
        if values:
            out[key] = out.get(key, ()) + values
    return out


def guidance(responses: Sequence[str]) -> Guidance:
    """S7 over a transcript."""
    merged: dict[str, tuple[str, ...]] = {}
    for r in responses:
        for key, values in structured_fields(r).items():
            merged[key] = merged.get(key, ()) + values
    return Guidance(merged)


def _named(value: str, prose: str) -> bool:
    return bool(value) and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", prose, re.I) is not None


def _follows_guidance(g: Guidance, prose: str, numbers: bool) -> tuple[bool, bool]:
    """S7: (declined, asked) as the structured fields say, given the reply's prose and whether it shows numbers."""
    f = g.fields
    declined = (
        any(_named(v, prose) for v in f.get("nearest_served", ()))
        or any(_named(v, prose) for v in f.get("served_elsewhere", ()))
        or (not numbers and any(k in f for k in ("outside_registry", "filter_matched_nothing")))
        or (
            not numbers
            and bool(g.next_step_classes() & {"DECLINE_NEAREST", "DECLINE_ELSEWHERE", "OUT_OF_SCOPE"})
        )
        or (not numbers and any(k not in FIELD_CLASS and k != "next_step" for k in f))
    )
    asked = not numbers and (
        any(_named(v, prose) for v in f.get("did_you_mean", ())) or "DISAMBIGUATE" in g.next_step_classes()
    )
    return declined, asked


def _successful(response: str) -> bool:
    return not ERROR_RESPONSE_RE.match(response.strip()) and not HUB_UNAVAILABLE_RE.search(response)


def _response_datasets(response: str, known: frozenset[str]) -> set[str]:
    ids = set(provenance_ids(response)) | {m for m in SOURCE_COMMENT_RE.findall(response) if m in known}
    for t in num.tables(response):
        cols = [h.strip().lower() for h in t.header]
        if "source_id" in cols and "can_answer" not in cols:  # suggest_datasets lists every dataset: skip it
            i = cols.index("source_id")
            ids |= {row[i].strip() for row in t.rows if row[i].strip()}
    return ids


def call_datasets(
    calls: Sequence[ToolCall], responses: Sequence[str], known: frozenset[str]
) -> frozenset[str]:
    """S8: the datasets the client's data and metadata calls resolved to, counting only calls that succeeded."""
    out: set[str] = set()
    for c, r in zip(calls, responses, strict=True):
        if c.tool not in DATA_TOOLS | METADATA_TOOLS or not _successful(r):
            continue
        source = c.args.get("source_id")
        if isinstance(source, str):
            out.add(source)
        sql = c.args.get("sql")
        if isinstance(sql, str):
            out |= {m.lower() for m in SQL_FROM_RE.findall(sql) if m.lower() in known}
        if c.tool != SQL_TOOL:
            out |= _response_datasets(r, known)
    return frozenset(out)


# ---------------------------------------------------------------------------------------------------------
# Target flags (T1-T3)

SUM_RE = re.compile(r"\bsum\s*\(", re.I)
PLACE_COL = r"(?:upper\s*\(\s*)?(?:states|location[12]?|area|reporting_area|state|place)\s*\)?"
PLACE_PRED_RE = re.compile(rf"{PLACE_COL}\s*(?:IN\s*\((?P<list>[^)]*)\)|=\s*'(?P<one>[^']*)')", re.I)
LABEL_COL = r"(?:upper\s*\(\s*|lower\s*\(\s*)?(?:label|condition)\s*\)?"
LABEL_IN_RE = re.compile(rf"{LABEL_COL}\s*IN\s*\((?P<list>[^)]*)\)", re.I)
LABEL_EQ_RE = re.compile(rf"{LABEL_COL}\s*=\s*'(?P<one>[^']*)'", re.I)
LABEL_LIKE_RE = re.compile(rf"{LABEL_COL}\s*I?LIKE\s*'(?P<pat>[^']*)'", re.I)
TOTAL_EXCLUDED_RE = re.compile(r"location2\s+IS\s+NULL|NOT\s+IN\s*\([^)]*(?:TOTAL|RESIDENTS)", re.I)
GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b(?P<cols>.*?)(?:\bORDER\b|\bLIMIT\b|\bHAVING\b|$)", re.I | re.S)


def _quoted(text: str) -> list[str]:
    return re.findall(r"'([^']*)'", text)


def _like_regex(pattern: str) -> re.Pattern[str]:
    parts = (".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.compile("".join(parts), re.I | re.S)


def sql_target_flags(sql: str, known_labels: Sequence[str]) -> frozenset[str]:
    """T1-T3 from one SQL statement: only a statement that SUMs can add rows that must stay apart."""
    if not SUM_RE.search(sql):
        return frozenset()
    flags: set[str] = set()
    group = GROUP_BY_RE.search(sql)
    group_cols = group.group("cols").lower() if group else ""
    grouped_by_place = re.search(r"states|location|area|place", group_cols) is not None
    grouped_by_label = re.search(r"label|condition", group_cols) is not None
    places = [
        p for m in PLACE_PRED_RE.finditer(sql) for p in (_quoted(m.group("list") or "") or [m.group("one")])
    ]
    places = [p for p in places if p is not None]
    if not grouped_by_place:
        aggregates = [p for p in places if num.is_aggregate(p)]
        if (aggregates and len(aggregates) < len(places)) or (
            not places and not TOTAL_EXCLUDED_RE.search(sql)
        ):
            flags.add("T1 total row added")
        if num.subarea_pair(places):
            flags.add("T3 sub-area added to its parent")
    if not grouped_by_label:
        labels = [x for m in LABEL_IN_RE.finditer(sql) for x in _quoted(m.group("list"))]
        labels += [m.group("one") for m in LABEL_EQ_RE.finditer(sql)]
        likes = [m.group("pat") for m in LABEL_LIKE_RE.finditer(sql)]
        like_hits = {lab for p in likes for lab in known_labels if _like_regex(p).fullmatch(lab)}
        if len({x.lower() for x in labels}) > 1 or len(like_hits) > 1:
            flags.add("T2 two series added")
    return frozenset(flags)


def _known_labels(responses: Sequence[str]) -> list[str]:
    labels: set[str] = set()
    for r in responses:
        for t in num.tables(r):
            cols = [h.strip().lower() for h in t.header]
            for name in ("label", "condition"):
                if name in cols:
                    labels |= {row[cols.index(name)].strip() for row in t.rows}
    return sorted(labels)


def target_flags(
    calls: Sequence[ToolCall],
    responses: Sequence[str],
    shown: Sequence[num.Token],
    fab: FabricationCheck,
    named: str = "",
) -> tuple[str, ...]:
    """T1-T3: from SQL whose response holds a shown number, and from how shown numbers were derived."""
    flags: set[str] = set()
    labels = _known_labels(responses)
    for c, r in zip(calls, responses, strict=True):
        sql = c.args.get("sql")
        if c.tool == SQL_TOOL and isinstance(sql, str) and any(_holds(r, t, named) for t in shown):
            flags |= sql_target_flags(sql, labels)
    names = {name for _, name in fab.forbidden}
    if "total_row_added" in names:
        flags.add("T1 total row added")
    if "series_added" in names:
        flags.add("T2 two series added")
    if fab.subarea_mixed:
        flags.add("T3 sub-area added to its parent")
    return tuple(sorted(flags))


def observe(
    expected: ExpectedRecord,
    calls: Sequence[ToolCall],
    responses: Sequence[str],
    answer: str,
    catalog_ids: frozenset[str] | None = None,
    fab: FabricationCheck | None = None,
) -> Observation:
    """Read the fixed signals (S1-S4, S7, S8, T1-T3) off one transcript and answer."""
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
    shown = _shown_tokens(answer)
    numbers = bool(shown)
    g = guidance(responses)
    if g.fields:  # S7 first: the fields decide, the phrases are not read
        declined, asked = _follows_guidance(g, prose, numbers)
    else:
        declined = any(p.search(prose) for p in DECLINE_PATTERNS)
        asked = any(_asks(s) for s in sentences) or any(p.search(prose) for p in ASK_PATTERNS)
    fitting |= {d for d in g.fields.get("served_elsewhere", ()) if d in ids and _named(d, prose)}
    fab = fab if fab is not None else check_fabrication(answer, calls, responses)
    named = named_text(answer, calls)
    return Observation(
        shown_numbers=tuple(t.plain for t in shown),
        declined=declined,
        handoff=HANDOFF_RE.search(prose) is not None,
        gbd_verdict=any(p.search(prose) for p in GBD_VERDICT_PATTERNS),
        asked_question=asked,
        cited_datasets=frozenset(cited),
        fitting_datasets=frozenset(fitting),
        named_places=frozenset(p for p in expected.expected_places if _place_named(p, prose)),
        sql_called=any(c.tool == SQL_TOOL for c in calls),
        called_datasets=call_datasets(calls, responses, ids),
        target_flags=target_flags(calls, responses, shown, fab, named),
        class_source="fields" if g.fields else "phrases",
        shown_tokens=tuple(shown),
        named_text=named,
        data_returned=data_returned(calls, responses),
        connectors=frozenset(name for name, pattern in CONNECTOR_RES.items() if pattern.search(prose)),
    )


def _asks(sentence: str) -> bool:
    """S2: a sentence asks a clarifying question when it ends in "?" and is not a follow-up offer."""
    return sentence.rstrip().endswith("?") and OFFER_RE.match(sentence) is None


def data_returned(calls: Sequence[ToolCall], responses: Sequence[str]) -> bool:
    """A data tool succeeded and returned at least one table row."""
    return any(
        c.tool in DATA_TOOLS and _successful(r) and any(t.rows for t in num.tables(r))
        for c, r in zip(calls, responses, strict=True)
    )


# ---------------------------------------------------------------------------------------------------------
# The three criteria


def class_score(expected: ExpectedRecord, obs: Observation) -> int:
    """R1, one fixed rule per expected class (numbers = substantive numbers shown, S1; phrases and fields, S2,
    S4 and S7).

    - ANSWER: 2 numbers shown, no decline and no clarifying question; 1 numbers with a decline or a question
      (it answers and also declines); 0 no numbers. Read from the fields when a response carries them; from the
      phrases only when no data tool returned rows, since a reply built on returned data that adds a caveat
      or a question is still an answer (S2).
    - DECLINE_NEAREST: 2 declines and gives numbers or names an expected nearest place (R8); 1 declines and offers
      nothing (framed as out of scope); 0 no decline.
    - DECLINE_ELSEWHERE: 2 declines and gives numbers or names a dataset as fitting; 1 declines and offers
      nothing; 0 no decline.
    - HANDOFF_GBD: 2 hands off to IHME/GBD with no numbers and no verdict; 1 hands off but also shows numbers or
      gives a "GBD vs local" verdict; 0 no hand-off (burden treated as observed counts, or nothing).
    - HANDOFF_CONNECTOR (C1): 0 names no connector; else, when `gbd` is a target, the HANDOFF_GBD rule (1 with
      numbers or a verdict, 2 without); for OpenAlex and PubMed targets, 2: the reply may list what this server
      holds (abstracts, years, DOIs) beside the hand-off, and the fabrication gate still checks every number.
      Which connector it named is Target's question, not Class's.
    - DISAMBIGUATE: 2 asks with no numbers; 1 asks after showing numbers (R7); 0 never asks.
    - OUT_OF_SCOPE: 2 declines with no numbers; 1 no numbers but asks or hands off instead of declining;
      0 shows numbers, or neither declines, asks nor hands off.
    """
    numbers = bool(obs.shown_numbers)
    match expected.expected_class:
        case "ANSWER":
            if not numbers:
                return 0
            lowered = obs.declined or obs.asked_question
            if obs.class_source == "phrases" and obs.data_returned:
                lowered = False
            return 1 if lowered else 2
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
        case "HANDOFF_CONNECTOR":
            if not obs.connectors:
                return 0
            if "gbd" in expected.connector_targets and (numbers or obs.gbd_verdict):
                return 1
            return 2
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
    """R2, the lower of a dataset part and a place part, capped at 1 by any target flag (T1-T3).

    Dataset part, with E = expected_datasets and F = datasets cited (provenance in the answer) or named
    outside a negation (S3), plus, when no number is shown, the datasets the calls resolved to (S8):
    - E empty (OUT_OF_SCOPE, HANDOFF_GBD; R6): 2 when the reply names none, else 0 (a dataset claimed to fit).
    - E set: 2 when every expected id is in F; 1 when some are; 0 when none are. Extra datasets named beside
      them don't lower the score (R5); numbers cited only from an unexpected dataset are 0.
    Place part, when expected_nearest is set: 2 all named, 1 some, 0 none (whole words, case-insensitive).
    Connector part, HANDOFF_CONNECTOR only (C1): 2 when the reply names a connector the record expects, else 0;
    its `expected_nearest` items that are not connectors are its places.
    """
    wanted = set(expected.expected_datasets)
    got = set(obs.fitting_datasets)
    if not wanted:
        dataset_part = 0 if got else 2
    else:
        if not obs.shown_numbers:
            got |= obs.called_datasets
        hit = wanted & got
        dataset_part = 2 if hit == wanted else 1 if hit else 0
    places = expected.expected_places
    score = dataset_part
    if places:
        named = len(obs.named_places)
        score = min(score, 2 if named == len(places) else 1 if named else 0)
    if expected.expected_class == "HANDOFF_CONNECTOR":
        score = min(score, 2 if obs.connectors & expected.connector_targets else 0)
    return min(score, 1) if obs.target_flags else score


def provenance_score(
    obs: Observation, answer: str, calls: Sequence[ToolCall], responses: Sequence[str]
) -> tuple[int, bool]:
    """R3: returns (score, not_applicable). No substantive number shown: N/A, which counts as 2.

    For each shown number, the sources are the datasets whose provenance line is in a response holding that
    number (N4, or an allowed sum of its cells, N6). Then:
    - 0 when some number has no source, or none of its sources has a provenance line in the answer;
    - 1 when every number has a line in the answer but one of those lines lacks a field (`provenance_complete`;
      every line the answer gives for a dataset is checked, and all must be complete),
      or when a source response says "provisional" and the answer doesn't (S6);
    - 2 otherwise.
    """
    del calls  # sources come from the responses; kept in the signature so every criterion reads the same case
    if not obs.shown_numbers:
        return 2, True
    text = _strip_placeholders(answer)
    lines: dict[str, list[str]] = {}
    for m in PROVENANCE_RE.finditer(text):
        lines.setdefault(m.group(1), []).append(m.group(0))
    score = 2
    for tok in obs.shown_tokens:
        holding = [r for r in responses if _holds(r, tok, obs.named_text)]
        sources = set().union(*(provenance_ids(r) for r in holding)) if holding else set()
        cited = sources & lines.keys()
        if not cited:
            return 0, False
        if not all(provenance_complete(line) for s in cited for line in lines[s]):
            score = 1
        if any(says_provisional(r) for r in holding) and not says_provisional(_prose(answer)):
            score = 1
    return score, False


def sql_needed(
    expected: ExpectedRecord, obs: Observation, calls: Sequence[ToolCall], responses: Sequence[str]
) -> bool:
    """S5: did this answer depend on `query_sql`? (The baseline's `sql_needed` flag, made a rule.)

    False when `query_sql` was not called. When it was: False only if the answer shows at least one substantive
    number and every one of them is held (N4/N6) by a response from another tool that carries provenance for an
    expected dataset; True otherwise (a candidate list or redirect built from a label search depends on SQL too).
    """
    if not obs.sql_called:
        return False
    if not obs.shown_tokens:
        return True
    other = [
        r
        for c, r in zip(calls, responses, strict=True)
        if c.tool != SQL_TOOL and provenance_ids(r) & set(expected.expected_datasets)
    ]
    return not all(any(_holds(r, t, obs.named_text) for r in other) for t in obs.shown_tokens)


# ---------------------------------------------------------------------------------------------------------
# Hosted run (H1-H5)


@dataclass(frozen=True)
class HostedSignals:
    """What a hosted transcript says about its data: the hub has no release, which datasets serve the asked
    condition as a series (a get_series table, empty, from that source), and the conditions it lists."""

    hub_unavailable: bool
    series_datasets: frozenset[str]
    available: tuple[str, ...]


def _split_top(text: str) -> list[str]:
    """Split a list on commas outside parentheses ("C. auris (screening, colonisation), Legionellosis")."""
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [x for x in out if x]


def hosted_signals(
    calls: Sequence[ToolCall], responses: Sequence[str], known: frozenset[str]
) -> HostedSignals:
    unavailable = any(HUB_UNAVAILABLE_RE.search(r) for r in responses)
    series: set[str] = set()
    available: list[str] = []
    for c, r in zip(calls, responses, strict=True):
        m = NO_SOURCE_RE.search(r)
        if m and m.group("list"):
            available += _split_top(m.group("list"))
        if c.tool in DATA_TOOLS - {SQL_TOOL} and not m and num.tables(r):
            series |= {i for i in SOURCE_COMMENT_RE.findall(r) if i in known} | _response_datasets(r, known)
        if c.tool == "describe_dataset":
            available += re.findall(r"^- ([^:\n]+):", r, re.M)
    return HostedSignals(unavailable, frozenset(series), tuple(dict.fromkeys(available)))


CANDIDATES_RE = re.compile(r"\bAsk:\s*(?P<list>[^?]*)", re.I)
STOP_WORDS = frozenset({"the", "and", "cases", "case", "which", "series", "disease", "diseases", "infection"})


def _words(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOP_WORDS)


def note_candidates(notes: str) -> tuple[frozenset[str], ...]:
    """H5: the candidates listed after "Ask:" in a record's notes, as word sets; unparsable items are dropped."""
    m = CANDIDATES_RE.search(notes)
    if not m:
        return ()
    items = re.split(r"\s*(?:,|;|\bor\b|\.\.\.|…)\s*", m.group("list"))
    return tuple(w for w in (_words(i) for i in items) if w)


def apply_hosted(
    expected: ExpectedRecord, obs: Observation, sig: HostedSignals
) -> tuple[Observation, str | None, int | None, int | None]:
    """H1-H5: returns the observation to score, the rule applied, and caps for Class and Target (None: no cap)."""
    if not sig.hub_unavailable:
        return obs, None, None, None
    wanted = set(expected.expected_datasets)
    served = wanted & sig.series_datasets
    match expected.expected_class:
        case "ANSWER":
            return obs, "H1", 0, 2 if wanted and served == wanted else 0
        case "DECLINE_NEAREST" | "DECLINE_ELSEWHERE":
            if served:
                return replace(obs, fitting_datasets=obs.fitting_datasets | served), "H2", None, None
            return obs, "H3", 1, 0
        case "DISAMBIGUATE":
            candidates = note_candidates(expected.notes)
            if not candidates:
                return obs, "H5", None, None
            listed = [_words(a) for a in sig.available]
            hits = sum(any(c <= a for a in listed) for c in candidates)
            if hits == 0:
                return obs, "H5", 0, None
            return obs, "H5", None, None if hits == len(candidates) else 1
        case _:
            return obs, "H4", None, None


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
    legacy: bool = False,
    hosted: bool = False,
) -> ScoredQuestion:
    """Score one question for protocols A and B. Pure: the same inputs always give the same result.

    `responses[i]` is the text `calls[i]` returned. With a `grader`, each criterion takes the stricter of the rule
    and the grader (FABRICATION: the grader's 0 marks it fabricated). `legacy=True` runs the baseline's substring
    fabrication check (fabcheck.py) instead of the token check, for comparison. `hosted=True` adds H1-H5.
    """
    if len(calls) != len(responses):
        raise ScorerError(f"{expected.id}: {len(calls)} calls but {len(responses)} responses")
    fab = check_fabrication(answer, calls, responses)
    if legacy:
        fab = FabricationCheck(unmatched_numbers_legacy(answer, calls, responses))
    obs = observe(expected, calls, responses, answer, catalog_ids, fab)
    rule: str | None = None
    class_cap = target_cap = None
    if hosted:
        known = known_dataset_ids(expected, responses, catalog_ids)
        obs, rule, class_cap, target_cap = apply_hosted(
            expected, obs, hosted_signals(calls, responses, known)
        )
    cls = class_score(expected, obs)
    tgt = target_score(expected, obs)
    if class_cap is not None:
        cls = min(cls, class_cap)
    if target_cap is not None:
        tgt = target_cap if rule == "H1" else min(tgt, target_cap)
    prov, na = provenance_score(obs, answer, calls, responses)
    fabricated = fab.fabricated
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
        unmatched_numbers=fab.unmatched,
        tools_called=tuple(c.tool for c in calls),
        derived_numbers=fab.derived,
        forbidden_derivations=fab.forbidden,
        target_flags=obs.target_flags,
        hosted_rule=rule,
        class_source=obs.class_source,
        sums_tried=fab.sums_tried,
    )


def _checked(score: int) -> int:
    if isinstance(score, bool) or score not in (0, 1, 2):
        raise ScorerError("a grader must return 0, 1 or 2")
    return score


# ---------------------------------------------------------------------------------------------------------
# Files


def load_expected(path: Path, split: Split, sha256: str | None = None) -> list[ExpectedRecord]:
    """Read a split file after checking its frozen sha256; a mismatch voids the run, so it raises.

    `sha256` replaces the frozen hash for a re-audited set; only `eval.sealed_run` passes it, from its reviewed
    list of re-audited TEST sets. Left out, the split's frozen hash applies.
    """
    want = sha256 if sha256 is not None else DEV_SHA256 if split == "dev" else TEST_SHA256
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

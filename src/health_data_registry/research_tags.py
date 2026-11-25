"""Our own rule-based tags for conference abstracts in the research index: fixed word rules, never a model.

The research index (CROI and the DOI-published conferences: IDWeek, SHEA Spring, APIC, ICID) tags every
abstract the same way, from its title and, where we hold it, its text. A tag is a hint for finding prior work,
not a fact about the study: a missing tag doesn't mean the topic is absent (ADR 0006: no model output is stored).

The same rules run in two places: here in Python for the DOI harvest, and as SQL in the shared staging
projection for sources that name the ``rule_tags`` extension (CROI's listing crawl). Every pattern is written
for both engines (Python's ``re`` and RE2): no look-around, no back-reference.

Rules ignore case, except acronyms that are also everyday words ("art", "prep", "pep", "tb", "sti"), which match
only as written in capitals, inside ``(?-i:...)``; a fellow's own search words are matched without case
(tools.py).
"""

from __future__ import annotations

import re

CONDITION_RULES: dict[str, tuple[str, ...]] = {
    "hiv": (r"\bhiv\b", r"\bpwh\b", r"\bplwh\b", r"antiretroviral", r"(?-i:\bART\b)"),
    "hiv_prevention": (
        r"(?-i:\bP[Rr][Ee]P\b)",
        r"pre-exposure prophylaxis",
        r"(?-i:\bPEP\b)",
        r"lenacapavir",
        r"cab-la",
    ),
    "hepatitis_b": (r"hepatitis b", r"\bhbv\b"),
    "hepatitis_c": (r"hepatitis c", r"\bhcv\b"),
    "tuberculosis": (r"tuberculosis", r"(?-i:\bTB\b)", r"mycobacterium"),
    "covid19": (r"covid", r"sars-cov-2"),
    "mpox": (r"\bmpox\b", r"monkeypox"),
    "syphilis": (r"syphilis",),
    "stis": (r"gonorr", r"chlamydia", r"sexually transmitted", r"(?-i:\bSTIs?\b)"),
    "cryptococcal": (r"cryptococ",),
    "hpv": (r"\bhpv\b", r"papillomavirus", r"anal cancer", r"cervical cancer"),
    "cmv": (r"\bcmv\b", r"cytomegalovirus"),
    "influenza": (r"\binfluenza",),  # not parainfluenza
    "rsv": (r"\brsv\b", r"respiratory syncytial"),
    "measles": (r"measles",),
    "c_difficile": (r"difficile", r"\bcdi\b"),
    "mrsa": (r"\bmrsa\b", r"methicillin-resistant"),
    "candida_auris": (r"candida auris", r"\bc\.? auris\b"),
    "cre": (r"\bcre\b", r"carbapenem-resistant", r"carbapenemase"),
    "clabsi": (r"\bclabsi\b", r"central line", r"\bcabsi\b"),
    "cauti": (r"\bcauti\b", r"catheter-associated urinary"),
    "ssi": (r"\bssi\b", r"surgical site infection"),
    "pneumonia": (r"pneumonia", r"\bvap\b", r"\bnv-hap\b"),
    # Word boundaries: "antiseptic", "aseptic" and "antisepsis" are not sepsis.
    "sepsis": (r"\bsepsis\b", r"\bseptic\b", r"bacteremia", r"bloodstream infection"),
}
DESIGN_RULES: dict[str, tuple[str, ...]] = {
    "randomized_trial": (r"randomi[sz]ed", r"\brct\b", r"placebo"),
    "cohort": (r"\bcohort\b", r"prospective", r"retrospective"),
    "cross_sectional": (r"cross-sectional",),
    "case_control": (r"case-control",),
    "modelling": (r"\bmodel(l)?ing\b", r"mathematical model", r"cost-effective"),
    "qualitative": (r"qualitative", r"in-depth interview", r"focus group"),
    "systematic_review": (r"systematic review", r"meta-analys"),
    "phylogenetic": (r"phylogen", r"whole-genome sequencing", r"\bwgs\b"),
    "quality_improvement": (r"quality improvement", r"\bpdsa\b"),
    "outbreak_investigation": (r"outbreak",),
}
DATA_SOURCE_RULES: dict[str, tuple[str, ...]] = {
    "surveillance": (r"surveillance", r"\bnndss\b", r"notifiable", r"\bnhsn\b"),
    "ehr": (r"electronic health record", r"\behr\b", r"medical records"),
    "claims": (r"\bclaims\b", r"medicaid", r"medicare", r"marketscan"),
    "registry": (r"\bregistry\b",),
    "national_survey": (r"\bnhanes\b", r"\bphia\b", r"demographic and health survey", r"\bdhs\b"),
    "cohort_consortium": (
        r"na-accord",
        r"\bcnics\b",
        r"\bieDEA\b",
        r"\bmacs\b",
        r"\bwihs\b",
        r"\breprieve\b",
    ),
}
_PLACES = (
    "United States", "USA", "Canada", "Mexico", "Brazil", "Peru", "Argentina", "Colombia", "Haiti",
    "United Kingdom", "UK", "France", "Spain", "Italy", "Germany", "Netherlands", "Switzerland", "Denmark",
    "Russia", "Ukraine", "China", "India", "Thailand", "Vietnam", "Cambodia", "Indonesia", "Japan", "Australia",
    "South Africa", "Kenya", "Uganda", "Tanzania", "Zambia", "Zimbabwe", "Malawi", "Mozambique", "Botswana",
    "Eswatini", "Lesotho", "Nigeria", "Ghana", "Cameroon", "Rwanda", "Ethiopia", "Côte d'Ivoire",
    "sub-Saharan Africa", "Europe", "Latin America", "Asia", "Malaysia", "Pakistan", "Bangladesh",
)  # fmt: skip
GEOGRAPHY_RULES: dict[str, tuple[str, ...]] = {
    p.lower().replace(" ", "_").replace("'", ""): (rf"\b{re.escape(p)}\b",) for p in _PLACES
}
_CASE_SENSITIVE_PLACES = frozenset({"usa", "uk"})


# The rule sets by name, each with the tags whose rules keep case (the rule_tags extension names them).
RULE_SETS: dict[str, tuple[dict[str, tuple[str, ...]], frozenset[str]]] = {
    "conditions": (CONDITION_RULES, frozenset()),
    "designs": (DESIGN_RULES, frozenset()),
    "data_sources": (DATA_SOURCE_RULES, frozenset()),
    "geography": (GEOGRAPHY_RULES, _CASE_SENSITIVE_PLACES),
}


def pattern(patterns: tuple[str, ...], *, case_sensitive: bool) -> str:
    """One tag's rules as one pattern, for RE2 (DuckDB's regexp_matches): the same matches as ``tags`` makes."""
    body = "|".join(f"(?:{p})" for p in patterns)
    return body if case_sensitive else f"(?i){body}"


def tags(
    rules: dict[str, tuple[str, ...]], text: str, *, case_sensitive: frozenset[str] = frozenset()
) -> str:
    """Every tag whose rule matches the text, sorted and joined with '; '."""
    hits = []
    for tag, patterns in rules.items():
        flags = 0 if tag in case_sensitive else re.I
        if any(re.search(p, text, flags) for p in patterns):
            hits.append(tag)
    return "; ".join(sorted(hits))


def tag_record(title: str, text: str | None) -> dict[str, str]:
    """The four tag columns of the research index for one abstract."""
    words = f"{title}\n{text or ''}"
    return {
        "condition_tags": tags(CONDITION_RULES, words),
        "design_tags": tags(DESIGN_RULES, words),
        "data_source_tags": tags(DATA_SOURCE_RULES, words),
        "geography_tags": tags(GEOGRAPHY_RULES, words, case_sensitive=_CASE_SENSITIVE_PLACES),
    }

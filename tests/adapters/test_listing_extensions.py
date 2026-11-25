"""The listing extensions on small hand-written fixtures: block_text, sidebar_fields and page_attributes (parse);
number_title_split, slug_to_words and rule_tags (staging). Each is pure, refuses rather than guesses, and keeps
every value as printed in the raw layer."""

from __future__ import annotations

from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from health_data_registry import research_tags
from health_data_registry.adapters.extensions import EXTENSIONS, ExtensionRefused, _load

_load()


def run_parse(name: str, args: dict[str, Any], html: str) -> dict[str, Any]:
    ext = EXTENSIONS[name]
    out = ext.run(ext.args_model.model_validate(args), html.encode(), "https://example.org/item/1/")
    assert len(out) == 1
    return dict(out[0])


def run_staging(name: str, args: dict[str, Any], rows: list[dict[str, str | None]]) -> list[dict[str, Any]]:
    ext = EXTENSIONS[name]
    cols = list(rows[0])
    con = duckdb.connect()
    con.execute("CREATE TABLE raw (" + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")")
    con.executemany(
        f"INSERT INTO raw VALUES ({', '.join('?' for _ in cols)})", [[r[c] for c in cols] for r in rows]
    )
    step = ext.run(ext.args_model.model_validate(args), "raw", cols)
    assert step.rows_per_row == 1
    cur = con.execute(f"SELECT * FROM {step.sql}")  # noqa: S608 - test SQL from the extension
    names = [d[0] for d in cur.description]
    assert tuple(names) == step.fields
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


# ------------------------------------------------------------------------------------------------- block_text

BODY = '<article><div class="entry-content"><h3>Background</h3><p>First  line,\n wrapped.</p>' \
    "<p>Second<br>and third.</p><script>x()</script><ul><li>one</li><li>two</li></ul></div></article>"  # fmt: skip


def test_block_text_keeps_paragraphs_as_lines() -> None:
    got = run_parse("block_text", {"fields": {"body": "div.entry-content"}}, BODY)
    # a line break in the page source starts a line too, as the retired CROI parser read it
    assert got == {"body": "Background\nFirst line,\nwrapped.\nSecond\nand third.\none\ntwo"}


@pytest.mark.parametrize("html", ["<p>none</p>", BODY + BODY])
def test_block_text_needs_exactly_one_element(html: str) -> None:
    with pytest.raises(ExtensionRefused, match="not one"):
        run_parse("block_text", {"fields": {"body": "div.entry-content"}}, html)


# ------------------------------------------------------------------------------------------------- sidebar_fields

SIDEBAR = {
    "block_selector": "div.side",
    "label_selector": "h4",
    "value_selector": "div.val",
    "labels": ["Abstract Number", "Authors", "Session Title"],
    "required": ["Abstract Number"],
}


def side(label: str, value: str) -> str:
    return f'<div class="side"><h4>{label}</h4><div class="val">{value}</div></div>'


def test_sidebar_fields_reads_labelled_blocks_as_printed() -> None:
    html = (
        side(" abstract  NUMBER ", "957")
        + side("Authors", "A. B,  C. D")
        + side("Other", "x")
        + "<div class='side'><h4>Lone</h4></div>"
    )
    assert run_parse("sidebar_fields", SIDEBAR, html) == {
        "Abstract Number": "957", "Authors": "A. B, C. D", "Session Title": None,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("html", "message"),
    [(side("Authors", "x"), "doesn't show 'Abstract Number'"), (side("Authors", "x") * 2 + side("Abstract Number", "1"), "twice")],
)  # fmt: skip
def test_sidebar_fields_refuses(html: str, message: str) -> None:
    with pytest.raises(ExtensionRefused, match=message):
        run_parse("sidebar_fields", SIDEBAR, html)


@pytest.mark.parametrize(
    "args",
    [
        {**SIDEBAR, "labels": ["A", "a"]},
        {**SIDEBAR, "required": ["Missing"]},
        {**SIDEBAR, "block_selector": "div:first-child"},
        {**SIDEBAR, "extra": 1},
    ],
)
def test_sidebar_fields_arguments_are_checked(args: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EXTENSIONS["sidebar_fields"].args_model.model_validate(args)


# ------------------------------------------------------------------------------------------------- page_attributes

ATTRS = {
    "fields": {
        "kind": {"selector": "article.abstract", "kind": "class_prefix", "class_prefix": "type-", "exactly_one": True},
        "poster": {"selector": "a[title]", "where": {"title": "Download Poster"}, "kind": "href",
                   "href_prefix": "https://example.org/"},
    }
}  # fmt: skip


def test_page_attributes_reads_class_tokens_and_links_as_written() -> None:
    html = (
        '<article class="abstract type-poster type-oral other"></article>'
        '<a title="Share" href="https://example.org/share">s</a>'
        '<a title="Download Poster" href="https://elsewhere.org/p.pdf">p</a>'
        '<a title="Download Poster" href="https://example.org/up/p-1.pdf">p</a>'
    )
    assert run_parse("page_attributes", ATTRS, html) == {
        "kind": "oral",
        "poster": "https://example.org/up/p-1.pdf",
    }
    bare = '<article class="abstract"></article>'
    assert run_parse("page_attributes", ATTRS, bare) == {"kind": None, "poster": None}


def test_page_attributes_refuses_a_page_without_its_one_element() -> None:
    with pytest.raises(ExtensionRefused, match="2 elements for kind"):
        run_parse("page_attributes", ATTRS, '<article class="abstract"></article>' * 2)


@pytest.mark.parametrize(
    "spec",
    [
        {"selector": "a", "kind": "href"},
        {"selector": "a", "kind": "href", "href_prefix": "http://example.org/"},
        {"selector": "a", "kind": "class_prefix"},
        {"selector": "a", "kind": "class_prefix", "class_prefix": "x-", "href_prefix": "https://e.org/"},
    ],
)
def test_page_attributes_arguments_are_checked(spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EXTENSIONS["page_attributes"].args_model.model_validate({"fields": {"f": spec}})


# ------------------------------------------------------------------------------------------------- number_title_split

SPLIT = {"field": "heading", "number_field": "number", "title_field": "title", "check_field": "side"}


@pytest.mark.parametrize(
    ("heading", "number", "title"),
    [
        ("957 – Lenacapavir  uptake", "957", "Lenacapavir uptake"),
        ("Y-12 — A title - with a dash", "Y-12", "A title - with a dash"),
        ("1.2 - Short", "1.2", "Short"),
    ],
)
def test_number_title_split(heading: str, number: str, title: str) -> None:
    got = run_staging("number_title_split", SPLIT, [{"heading": heading, "side": number}])
    assert got == [{"heading": heading, "side": number, "number": number, "title": title}]


@pytest.mark.parametrize(
    ("heading", "side", "message"),
    [("No number here", "1", "isn't"), (None, "1", "isn't"), ("957 – T", "958", "differs")],
)
def test_number_title_split_stops_the_load(heading: str | None, side: str, message: str) -> None:
    with pytest.raises(duckdb.Error, match=message):
        run_staging("number_title_split", SPLIT, [{"heading": heading, "side": side}])


def test_number_title_split_refuses_a_changed_layout() -> None:
    ext = EXTENSIONS["number_title_split"]
    args = ext.args_model.model_validate(SPLIT)
    with pytest.raises(ExtensionRefused, match="no longer have side"):
        ext.run(args, "raw", ["heading"])
    with pytest.raises(ExtensionRefused, match="overwritten"):
        ext.run(args, "raw", ["heading", "side", "title"])
    for bad in (
        {**SPLIT, "title_field": "heading"},
        {**SPLIT, "pattern": r"(\d+)"},
        {**SPLIT, "pattern": r"(a+)+\1"},
    ):
        with pytest.raises(ValidationError):
            ext.args_model.model_validate(bad)


# ------------------------------------------------------------------------------------------------- slug_to_words


def test_slug_to_words() -> None:
    got = run_staging(
        "slug_to_words",
        {"fields": ["kind"]},
        [{"kind": "hiv-prevention", "other": "a-b"}, {"kind": None, "other": None}],
    )
    assert got == [{"kind": "hiv prevention", "other": "a-b"}, {"kind": None, "other": None}]
    ext = EXTENSIONS["slug_to_words"]
    with pytest.raises(ExtensionRefused):
        ext.run(ext.args_model.model_validate({"fields": ["gone"]}), "raw", ["kind"])


# ------------------------------------------------------------------------------------------------- rule_tags

TAGS = {
    "text_fields": ["title", "body"],
    "columns": {"c": "conditions", "d": "designs", "s": "data_sources", "g": "geography"},
}
TEXTS = [
    ("Lenacapavir PrEP in Kenya", "A randomized trial; surveillance in the USA."),
    ("state of the art in prep work", "usa lower case, tb in lower case"),
    ("TB and ART in South Africa", "A retrospective cohort; Côte d'Ivoire; STIs; parainfluenza"),
    ("Nothing to tag", None),
]


@pytest.mark.parametrize(("title", "body"), TEXTS)
def test_rule_tags_in_sql_match_research_tags_in_python(title: str, body: str | None) -> None:
    got = run_staging("rule_tags", TAGS, [{"title": title, "body": body}])[0]
    want = research_tags.tag_record(title, body)
    assert (got["c"], got["d"], got["s"], got["g"]) == (
        want["condition_tags"], want["design_tags"], want["data_source_tags"], want["geography_tags"],
    )  # fmt: skip


def test_rule_tags_acronyms_only_in_capitals() -> None:
    assert research_tags.tag_record("state of the art; prep; tb", None)["condition_tags"] == ""
    assert (
        research_tags.tag_record("ART, PrEP, TB", None)["condition_tags"]
        == "hiv; hiv_prevention; tuberculosis"
    )


def test_rule_tags_arguments_and_layout_are_checked() -> None:
    ext = EXTENSIONS["rule_tags"]
    with pytest.raises(ValidationError):
        ext.args_model.model_validate({**TAGS, "columns": {"title": "conditions"}})
    with pytest.raises(ValidationError):
        ext.args_model.model_validate({**TAGS, "columns": {"c": "moods"}})
    args = ext.args_model.model_validate(TAGS)
    with pytest.raises(ExtensionRefused, match="no longer have body"):
        ext.run(args, "raw", ["title"])
    with pytest.raises(ExtensionRefused, match="overwritten"):
        ext.run(args, "raw", ["title", "body", "c"])

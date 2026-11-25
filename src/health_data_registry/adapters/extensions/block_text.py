"""block_text (hook parse): an element's text with its paragraphs kept as lines, as a reader sees them.

The listing config's ``fields`` read text with every run of white space made one space. A body of several
paragraphs ("Background", "Methods", ...) loses its structure that way; this reads the one element each selector
names with a line break at every paragraph, heading, list item, row and line break, spaces inside a line
collapsed, and empty lines dropped (a line break in the page source starts a line too). Script and style text is
left out. No word is changed or dropped.

Refused: a selector that doesn't match exactly one element on the page.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import Field, field_validator

from health_data_registry.adapters.extensions import Extension, ExtensionRefused, register
from health_data_registry.adapters.html import SKIP_TEXT, Element, parse_html, select
from health_data_registry.source_config import Strict, check_selector

LINE_BREAKS: Final = frozenset({"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"})


class Args(Strict):
    fields: dict[str, str] = Field(min_length=1, max_length=20)  # field name -> selector

    @field_validator("fields")
    @classmethod
    def _selectors(cls, v: dict[str, str]) -> dict[str, str]:
        for sel in v.values():
            check_selector(sel)
        return v


def lines(el: Element) -> str:
    """The element's text, one line per block, spaces inside a line collapsed, empty lines dropped."""
    parts: list[str] = []

    def walk(node: Element) -> None:
        if node.tag in SKIP_TEXT:
            return
        block = node.tag in LINE_BREAKS
        if block:
            parts.append("\n")
        for c in node.children:
            if isinstance(c, str):
                parts.append(c)
            else:
                walk(c)
        if block:
            parts.append("\n")

    walk(el)
    raw = "".join(parts).replace("\xa0", " ")
    out = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
    return "\n".join(line for line in out if line)


def parse(args: Args, content: bytes, url: str) -> list[dict[str, str]]:
    doc = parse_html(content)
    rec: dict[str, str] = {}
    for name, selector in args.fields.items():
        hits = select(doc, selector)
        if len(hits) != 1:
            raise ExtensionRefused(f"{url} has {len(hits)} elements for {name}, not one")
        rec[name] = lines(hits[0])
    return [rec]


EXTENSION = register(
    Extension(
        name="block_text",
        hook="parse",
        args_model=Args,
        run=parse,
        summary="One element's text with its paragraphs kept as lines; no word changed or dropped.",
    )
)

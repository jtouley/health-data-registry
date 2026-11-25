"""page_attributes (hook parse): facts an item page carries in attributes, not text, read as written.

Two kinds, each a named field:

- ``class_prefix``: the one element the selector matches carries a class token starting with the prefix; the
  value is the rest of the token as written (``abstract-category-hiv-prevention`` with prefix
  ``abstract-category-`` gives ``hiv-prevention``). Several such tokens: the first in sorted order. None: empty.
- ``href``: the first element the selector matches whose ``href`` starts with ``href_prefix``; the value is that
  link as written. None: empty. A link is only kept, never fetched (it may point to a file the source never reads).

``where`` keeps only elements whose attributes have exactly the given values (a link titled "Download Poster").
``exactly_one: true`` refuses a page where the selector doesn't match exactly one element, so a changed page is
refused rather than read from the wrong element. Nothing is renamed, computed or dropped.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from health_data_registry.adapters.extensions import Extension, ExtensionRefused, register
from health_data_registry.adapters.html import parse_html, select
from health_data_registry.source_config import Strict, check_selector


class Attribute(Strict):
    selector: str
    kind: Literal["class_prefix", "href"]
    class_prefix: str | None = Field(default=None, min_length=1, max_length=100)
    href_prefix: str | None = Field(default=None, pattern=r"^https://[^/\s]+/")
    exactly_one: bool = False
    # Exact attribute values the element must also have (a link's title, "Download Poster"): for values the
    # selector syntax can't hold, such as text with spaces.
    where: dict[str, str] = Field(default_factory=dict, max_length=5)

    @field_validator("selector")
    @classmethod
    def _selector(cls, v: str) -> str:
        return check_selector(v)

    @model_validator(mode="after")
    def _kind(self) -> Attribute:
        if (self.kind == "class_prefix") != (self.class_prefix is not None):
            raise ValueError("class_prefix is given exactly for kind class_prefix")
        if (self.kind == "href") != (self.href_prefix is not None):
            raise ValueError("href_prefix is given exactly for kind href")
        return self


class Args(Strict):
    fields: dict[str, Attribute] = Field(min_length=1, max_length=20)


def parse(args: Args, content: bytes, url: str) -> list[dict[str, str | None]]:
    doc = parse_html(content)
    rec: dict[str, str | None] = {}
    for name, spec in args.fields.items():
        hits = [
            el
            for el in select(doc, spec.selector)
            if all(el.attrs.get(k) == v for k, v in spec.where.items())
        ]
        if spec.exactly_one and len(hits) != 1:
            raise ExtensionRefused(f"{url} has {len(hits)} elements for {name}, not one")
        value: str | None = None
        if spec.kind == "class_prefix":
            assert spec.class_prefix is not None  # noqa: S101 - the model requires it for this kind
            tokens = sorted(
                t[len(spec.class_prefix) :]
                for el in hits[:1]
                for t in el.classes()
                if t.startswith(spec.class_prefix) and len(t) > len(spec.class_prefix)
            )
            value = tokens[0] if tokens else None
        else:
            assert spec.href_prefix is not None  # noqa: S101 - the model requires it for this kind
            links = [el.attrs.get("href", "") for el in hits]
            value = next((h for h in links if h.startswith(spec.href_prefix)), None)
        rec[name] = value
    return [rec]


EXTENSION = register(
    Extension(
        name="page_attributes",
        hook="parse",
        args_model=Args,
        run=parse,
        summary="Facts an item page carries in a class token or a link, read as written; links are kept, not fetched.",
    )
)

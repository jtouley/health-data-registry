"""page_fields (hook parse): fields an item page gives in ways one CSS selector's text can't, read as printed.

Three generic readers, each declared by its arguments and each reading exactly what the page prints:

- ``labelled``: blocks that each pair a heading with a value (a sidebar of "Abstract Number" / "957", "Authors" /
  "..."). Each block matching ``block_selector`` is read as its ``heading_selector`` text and its
  ``value_selector`` text; a field takes the value under the heading it names (compared without case or edge
  spaces). A heading printed twice is refused; a heading the page doesn't print leaves the field empty.
- ``links``: the ``href`` of the one link a selector names (a "Download Poster" link), resolved nowhere: the URL
  as printed, so a poster is linked, never fetched.
- ``class_slugs``: the part after a prefix of a class on the one element a selector names
  (``abstract-session-type-poster`` gives ``poster``), as printed.

One page gives one record. Nothing is computed, renamed or filled in; the core's row guard checks the record.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from health_data_registry.adapters.extensions import Extension, ExtensionLayoutRefused, register
from health_data_registry.adapters.html import Element, parse_html, select
from health_data_registry.source_config import Strict, check_selector

_FIELD = Field(min_length=1, max_length=20)


def _name_ok(name: str) -> bool:
    return name.isidentifier() and name.isascii() and name == name.lower() and 2 <= len(name) <= 41


class Labelled(Strict):
    block_selector: str
    heading_selector: str
    value_selector: str
    fields: dict[str, str] = _FIELD  # field name -> the heading it is printed under

    @field_validator("block_selector", "heading_selector", "value_selector")
    @classmethod
    def _sel(cls, v: str) -> str:
        return check_selector(v)


class Link(Strict):
    selector: str
    title: str | None = (
        None  # only the matching links whose title attribute is exactly this ("Download Poster")
    )

    @field_validator("selector")
    @classmethod
    def _sel(cls, v: str) -> str:
        return check_selector(v)


class ClassSlug(Strict):
    selector: str
    prefix: str = Field(min_length=1, max_length=80)

    @field_validator("selector")
    @classmethod
    def _sel(cls, v: str) -> str:
        return check_selector(v)


class Args(Strict):
    labelled: Labelled | None = None
    links: dict[str, Link] = Field(default_factory=dict)  # field name -> the one link it reads
    class_slugs: dict[str, ClassSlug] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names(self) -> Args:
        names = [*(self.labelled.fields if self.labelled else {}), *self.links, *self.class_slugs]
        if not names:
            raise ValueError("page_fields reads at least one field")
        bad = [n for n in names if not _name_ok(n)]
        if bad:
            raise ValueError(f"a field's name is lower case letters, digits and _ ({', '.join(bad)})")
        if len(set(names)) != len(names):
            raise ValueError("each field is read once")
        return self


def _one(root: Element, selector: str, what: str, url: str) -> Element | None:
    hits = select(root, selector)
    if len(hits) > 1:
        raise ExtensionLayoutRefused(f"{url} has {len(hits)} elements for {what}, not one")
    return hits[0] if hits else None


def read(args: Args, content: bytes, url: str) -> list[dict[str, str | None]]:
    root = parse_html(content)
    rec: dict[str, str | None] = {}
    if args.labelled is not None:
        lab = args.labelled
        printed: dict[str, str] = {}
        for block in select(root, lab.block_selector):
            heads, values = select(block, lab.heading_selector), select(block, lab.value_selector)
            if len(heads) != 1 or len(values) != 1:
                continue  # not a heading-and-value block
            key = heads[0].text().strip().casefold()
            if key in printed:
                raise ExtensionLayoutRefused(f"{url} prints the heading {heads[0].text()!r} twice")
            printed[key] = values[0].text()
        for name, heading in lab.fields.items():
            rec[name] = printed.get(heading.strip().casefold())
    for name, link in args.links.items():
        hits = [
            e for e in select(root, link.selector) if link.title is None or e.attrs.get("title") == link.title
        ]
        if len(hits) > 1:
            raise ExtensionLayoutRefused(f"{url} has {len(hits)} links for {name}, not one")
        rec[name] = (hits[0].attrs.get("href") or None) if hits else None
    for name, slug in args.class_slugs.items():
        el = _one(root, slug.selector, name, url)
        classes = el.classes() if el is not None else set()
        found = sorted(c[len(slug.prefix) :] for c in classes if c.startswith(slug.prefix))
        if len(found) > 1:
            raise ExtensionLayoutRefused(f"{url} has {len(found)} classes for {name}, not one")
        rec[name] = found[0] if found and found[0] else None
    return [rec]


EXTENSION = register(
    Extension(
        name="page_fields",
        hook="parse",
        args_model=Args,
        run=read,
        summary="Fields an item page prints as heading-and-value blocks, link targets or class slugs, as printed.",
    )
)

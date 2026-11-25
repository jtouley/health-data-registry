"""sidebar_fields (hook parse): label-and-value blocks on an item page, read as the page prints them.

Many item pages show their facts as small blocks, each a heading and its value ("Authors" above the author list).
This reads every block the ``block_selector`` matches, takes its one label (``label_selector``) and one value
(``value_selector``), and keeps the blocks whose label is one of ``labels`` (compared without case and outer
spaces). The record is keyed by the label as the catalog spells it, the value is the element's text as printed.

Refused, never guessed: a kept label shown twice, or a ``required`` label the page doesn't show. A block without
exactly one label and one value is not a fact block and is skipped. Nothing is renamed, computed or dropped.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from health_data_registry.adapters.extensions import Extension, ExtensionRefused, register
from health_data_registry.adapters.html import parse_html, select
from health_data_registry.source_config import Strict, check_selector


class Args(Strict):
    block_selector: str
    label_selector: str
    value_selector: str
    labels: list[str] = Field(min_length=1, max_length=50)
    required: list[str] = Field(default_factory=list)

    @field_validator("block_selector", "label_selector", "value_selector")
    @classmethod
    def _selectors(cls, v: str) -> str:
        return check_selector(v)

    @model_validator(mode="after")
    def _labels(self) -> Args:
        keys = [_key(label) for label in self.labels]
        if any(not k for k in keys) or len(set(keys)) != len(keys):
            raise ValueError("labels are non-empty and each is listed once")
        if not {_key(r) for r in self.required} <= set(keys):
            raise ValueError("every required label is one of labels")
        return self


def _key(label: str) -> str:
    return " ".join(label.split()).lower()


def parse(args: Args, content: bytes, url: str) -> list[dict[str, str | None]]:
    doc = parse_html(content)
    wanted = {_key(label): label for label in args.labels}
    found: dict[str, str] = {}
    for block in select(doc, args.block_selector):
        heads, values = select(block, args.label_selector), select(block, args.value_selector)
        if len(heads) != 1 or len(values) != 1:
            continue
        label = wanted.get(_key(heads[0].text()))
        if label is None:
            continue
        if label in found:
            raise ExtensionRefused(f"{url} shows {label!r} twice")
        found[label] = values[0].text()
    missing = [r for r in args.required if wanted[_key(r)] not in found]
    if missing:
        raise ExtensionRefused(f"{url} doesn't show {', '.join(repr(m) for m in missing)}")
    return [{label: found.get(label) for label in args.labels}]


EXTENSION = register(
    Extension(
        name="sidebar_fields",
        hook="parse",
        args_model=Args,
        run=parse,
        summary="Label-and-value blocks on an item page, keyed by the declared labels, values as printed.",
    )
)

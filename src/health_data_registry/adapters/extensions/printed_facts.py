"""printed_facts (hook parse): facts a document prints once, read from its text exactly as printed.

A report prints some facts once for the whole document: its title year, the area it says it covers, the date its
data were retrieved. Each fact is a named pattern with one group, searched in the document's text (its words as
lines, in reading order); the first match's group is the fact, as printed, and a pattern that matches nothing gives
an empty fact, never a guess. What a fact means (which area, which year) is the contract's business, not this
step's. One document gives one record of facts, which the type adds to each of its rows.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from health_data_registry.adapters.extensions import Extension, register
from health_data_registry.source_config import MAX_MATCH_INPUT, Strict, compile_pattern, safe_pattern

# A document's text is searched line by line, each line bounded like every catalogued match.
MAX_LINES = 20_000


class Args(Strict):
    facts: dict[str, str] = Field(min_length=1, max_length=20)  # field name -> pattern with exactly one group

    @field_validator("facts")
    @classmethod
    def _facts(cls, v: dict[str, str]) -> dict[str, str]:
        for name, pattern in v.items():
            if not (name.isidentifier() and name.isascii() and name == name.lower() and 2 <= len(name) <= 41):
                raise ValueError(f"a fact's name is lower case letters, digits and _ ({name!r})")
            safe_pattern(pattern, groups=1)
        return v


def read(args: Args, content: bytes, url: str) -> list[dict[str, str | None]]:
    del url  # the facts come from the text alone
    text_lines = content.decode("utf-8", errors="replace").splitlines()[:MAX_LINES]
    found: dict[str, str | None] = {}
    for name, pattern in args.facts.items():
        compiled = compile_pattern(pattern)
        found[name] = None
        for line in text_lines:
            m = compiled.search(line[:MAX_MATCH_INPUT])
            if m is not None and m.group(1):
                found[name] = m.group(1)
                break
    return [found]


EXTENSION = register(
    Extension(
        name="printed_facts",
        hook="parse",
        args_model=Args,
        run=read,
        summary="Facts a document prints once (title year, area, retrieval date), each by a one-group pattern.",
    )
)

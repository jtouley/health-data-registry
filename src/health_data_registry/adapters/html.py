"""A small, safe HTML reader: a tree from the standard library's parser, and a few CSS selectors.

Server-rendered pages only: no script runs, nothing is loaded from the network, and entities are decoded by the
standard parser (HTML has no external entities). Supported selectors, enough to name a table or a field:

    tag   #id   .class   [attr]   [attr="value"]   compounds of these (``table#cases.wide``)
    descendant (``div.entry h1``) and child (``ul > li``) combinators, and comma-separated groups

Anything else is refused when the catalog loads (``parse_selector``), never half-matched.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

VOID: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
SKIP_TEXT: Final = frozenset({"script", "style", "template", "noscript"})
BREAKS: Final = frozenset(
    {
        "br",
        "p",
        "div",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "dd",
        "dt",
    }
)
MAX_DEPTH: Final = 512
MAX_NODES: Final = 2_000_000


class SelectorError(ValueError):
    """A selector this reader does not support."""


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    children: list[Element | str] = field(default_factory=list)
    parent: Element | None = None

    def iter(self) -> Iterator[Element]:
        """This element and every element below it, in document order."""
        stack: list[Element] = [self]
        while stack:
            el = stack.pop()
            yield el
            stack.extend(reversed([c for c in el.children if isinstance(c, Element)]))

    def text(self) -> str:
        """The element's text content with whitespace runs collapsed to one space (script and style left out)."""
        parts: list[str] = []
        stack: list[Element | str] = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                parts.append(node)
            elif node.tag not in SKIP_TEXT:
                # a line break or a block's edge separates words, as a browser shows them
                gap = [" "] if node.tag in BREAKS else []
                stack.extend([*gap, *reversed(node.children), *gap])
        return " ".join("".join(parts).split())

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {})
        self.open: list[Element] = [self.root]
        self.nodes = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES or len(self.open) > MAX_DEPTH:
            raise SelectorError("the page is too large or too deeply nested to read")
        el = Element(tag, {k: v or "" for k, v in attrs}, parent=self.open[-1])
        self.open[-1].children.append(el)
        if tag not in VOID:
            self.open.append(el)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.open.pop()

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.open) - 1, 0, -1):
            if self.open[i].tag == tag:
                del self.open[i:]
                return

    def handle_data(self, data: str) -> None:
        self.open[-1].children.append(data)


def parse_html(content: bytes | str) -> Element:
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    b = _Builder()
    b.feed(text)
    b.close()
    return b.root


_COMPOUND: Final = re.compile(
    r"(?P<tag>[a-zA-Z][a-zA-Z0-9-]*|\*)?(?P<rest>(?:#[\w-]+|\.[\w-]+|\[[\w-]+(?:=\"[^\"]*\")?\])*)"
)
_PART: Final = re.compile(r"#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=\"([^\"]*)\")?\]")


@dataclass(frozen=True)
class Compound:
    tag: str | None
    id: str | None
    classes: tuple[str, ...]
    attrs: tuple[tuple[str, str | None], ...]

    def matches(self, el: Element) -> bool:
        if self.tag is not None and el.tag != self.tag:
            return False
        if self.id is not None and el.attrs.get("id") != self.id:
            return False
        if not set(self.classes) <= el.classes():
            return False
        return all(k in el.attrs and (v is None or el.attrs[k] == v) for k, v in self.attrs)


Selector = tuple[tuple[str, Compound], ...]  # (combinator before it: "" first, " " or ">") per compound


def _compound(text: str) -> Compound:
    m = _COMPOUND.fullmatch(text)
    if m is None or not text:
        raise SelectorError(f"{text!r} is not a supported selector")
    tag = m.group("tag")
    ident, classes, attrs = None, [], []
    for p in _PART.finditer(m.group("rest") or ""):
        if p.group(1):
            ident = p.group(1)
        elif p.group(2):
            classes.append(p.group(2))
        else:
            attrs.append((p.group(3), p.group(4)))
    return Compound(None if tag in (None, "*") else tag.lower(), ident, tuple(classes), tuple(attrs))


def parse_selector(text: str) -> list[Selector]:
    """The selector groups of ``text``; SelectorError for anything outside the supported subset."""
    groups = []
    for group in text.split(","):
        tokens = re.sub(r"\s*>\s*", " > ", group.strip()).split()
        if not tokens or tokens[0] == ">" or tokens[-1] == ">":
            raise SelectorError(f"{text!r} is not a supported selector")
        parts: list[tuple[str, Compound]] = []
        comb = ""
        for tok in tokens:
            if tok == ">":
                if comb == ">":
                    raise SelectorError(f"{text!r} is not a supported selector")
                comb = ">"
                continue
            parts.append((comb if parts else "", _compound(tok)))
            comb = " "
        groups.append(tuple(parts))
    return groups


def _matches(el: Element, sel: Selector) -> bool:
    comb, last = sel[-1]
    if not last.matches(el):
        return False
    if len(sel) == 1:
        return True
    rest = sel[:-1]
    parent = el.parent
    if comb == ">":
        return parent is not None and _matches(parent, rest)
    while parent is not None:
        if _matches(parent, rest):
            return True
        parent = parent.parent
    return False


def select(root: Element, selector: str) -> list[Element]:
    """Every element under ``root`` that a selector group matches, in document order, each once."""
    groups = parse_selector(selector)
    return [el for el in root.iter() if el is not root and any(_matches(el, g) for g in groups)]

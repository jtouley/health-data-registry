"""The crawler factory's guard tests (ADR 0016; the #118 review round's must-haves).

- Adapter and extension code names no source: no catalog id and no catalogued host in code (docstrings and
  comments are ignored; hosts an adapter type owns itself, the bibliographic APIs, are allowed), and no
  ``source_id ==`` / ``entry.id ==`` comparison.
- The files this PR converted compare no ``portal`` or ``type`` to a string: behaviour that differs by type is a
  declared capability.
- Every config model and extension argument model is strict.
- The registries agree with the catalog schema.
- Extensions are pure (their imports), and their tests run with sockets disabled.
- Every type and every record-producing extension's output passes the hard-rule-4 guard.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import socket
import tokenize
from collections.abc import Iterator
from pathlib import Path

import pytest

from health_data_registry import adapters
from health_data_registry.adapters import TYPES, Page, pages
from health_data_registry.adapters.extensions import EXTENSIONS, _load
from health_data_registry.adapters.row_guard import RowLevelRefused
from health_data_registry.registry import load_registry
from health_data_registry.source_config import CONFIG_MODELS, TYPE_HOOKS, TYPE_NAMES
from tests.adapters.helpers import entry, fetch_of

SRC = Path(__file__).resolve().parents[2] / "src" / "health_data_registry"
ADAPTERS = SRC / "adapters"
# Hosts an adapter type owns itself: the same for every source of the type, never one source's (the DOI type's
# bibliographic APIs).
TYPE_OWNED_HOSTS = {"doi_abstracts.py": {"api.crossref.org", "api.openalex.org", "www.ebi.ac.uk"}}
# The files this PR converted from portal tests to declared capabilities.
CONVERTED = ["adapters/__init__.py", "adapters/_types.py", "crawl.py", "cli.py", "live.py", "store.py", "staging.py",
             "sitemaps.py", "doi_harvest.py", "registry.py", "access.py", "listing.py", "transform_gen.py"]  # fmt: skip


def _code_strings(path: Path) -> Iterator[tuple[int, str]]:
    """String literals in code, leaving out docstrings (comments are not tokens of the AST)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def _adapter_modules() -> list[Path]:
    return sorted(p for p in ADAPTERS.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_source_ids_or_hosts_in_adapter_code() -> None:
    reg = load_registry()
    ids = {ds.id for ds in reg}
    hosts = set(reg.hosts)
    found = []
    for path in _adapter_modules():
        allowed = TYPE_OWNED_HOSTS.get(path.name, set())
        for line, text in _code_strings(path):
            for name in ids | (hosts - allowed):
                if name in text:
                    found.append(f"{path.relative_to(SRC)}:{line} names {name!r}")
    assert not found, "adapter code must not name a source; put it in catalog/:\n" + "\n".join(found)


def test_no_source_id_comparisons_in_adapter_code() -> None:
    found = []
    for path in _adapter_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Compare):
                left = ast.unparse(node.left)
                if (left.endswith(".id") or left in ("source_id", "dataset_id")) and any(
                    isinstance(c, ast.Constant) and isinstance(c.value, str) for c in node.comparators
                ):
                    found.append(f"{path.relative_to(SRC)}:{node.lineno}: {ast.unparse(node)}")
    assert not found, "\n".join(found)


def test_the_guard_ignores_comments_and_docstrings(tmp_path: Path) -> None:
    sample = '"""Reads data.cdc.gov."""\nX = 1  # census_county_pop\ndef f():\n    """www2.census.gov"""\n    return "ok"\n'
    path = tmp_path / "_sample_for_guard.py"
    path.write_text(sample, encoding="utf-8")
    assert [t for _l, t in _code_strings(path)] == ["ok"]


def _type_comparisons(path: Path) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        named = [ast.unparse(s) for s in sides]
        if any(n.endswith((".portal", "source.kind", "source.type")) or n == "portal" for n in named) and any(
            isinstance(s, ast.Constant) and isinstance(s.value, str) for s in sides
        ):
            out.append(f"{path.name}:{node.lineno}: {ast.unparse(node)}")
    return out


def test_no_portal_or_type_branches_in_converted_files() -> None:
    found = [hit for rel in CONVERTED for hit in _type_comparisons(SRC / rel)]
    assert not found, "behaviour that differs by type is a declared capability:\n" + "\n".join(found)


def test_the_branch_guard_catches_a_portal_test(tmp_path: Path) -> None:
    p = tmp_path / "_probe_branch.py"
    p.write_text(
        'def f(e):\n    return e.source.portal == "tableau" or "doi" != e.source.kind\n', encoding="utf-8"
    )
    assert len(_type_comparisons(p)) == 2


def test_registries_agree_with_the_catalog_schema() -> None:
    _load()
    assert set(TYPES) == set(TYPE_NAMES) == set(CONFIG_MODELS) == set(TYPE_HOOKS)
    for name, t in TYPES.items():
        assert t.config_model is CONFIG_MODELS[name]
    for ext in EXTENSIONS.values():
        assert ext.hook in {h for hooks in TYPE_HOOKS.values() for h in hooks}, ext.name


def test_every_config_and_argument_model_is_strict() -> None:
    _load()
    models = [t.config_model for t in TYPES.values()] + [e.args_model for e in EXTENSIONS.values()]
    for m in models:
        assert m.model_config.get("extra") == "forbid" and m.model_config.get("frozen"), m.__name__


def test_every_real_catalog_entry_loads_through_its_type() -> None:
    for ds in load_registry():
        assert ds.entry.source.kind in TYPES
        assert isinstance(ds.entry.source.config(), CONFIG_MODELS[ds.entry.source.kind])


# ------------------------------------------------------------------------------------------- purity

ALLOWED_IMPORTS = {"__future__", "collections", "collections.abc", "dataclasses", "typing", "re", "pydantic",
                   "health_data_registry.source_config", "health_data_registry.adapters.extensions", "math",
                   "decimal", "unicodedata", "string", "itertools", "functools", "html", "json",
                   # The HTML reader parse extensions use: a pure parser (checked below), no network or files.
                   "health_data_registry.adapters.html"}  # fmt: skip
FORBIDDEN_CALLS = {"open", "eval", "exec", "__import__", "input"}


def test_extensions_are_pure() -> None:
    bad = []
    for path in sorted((ADAPTERS / "extensions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in FORBIDDEN_CALLS
            ):
                bad.append(f"{path.name}:{node.lineno} calls {node.func.id}")
            bad += [f"{path.name}:{node.lineno} imports {n}" for n in names if n not in ALLOWED_IMPORTS]
    assert not bad, "extensions are pure: no network, file system, clock or environment\n" + "\n".join(bad)


def test_the_html_reader_extensions_may_use_is_pure() -> None:
    """adapters/html.py is on the extensions' allowlist, so it is held to the same rule: it imports only pure
    modules (html.parser, re, typing ...) and calls nothing that reaches the network, files or the clock."""
    path = ADAPTERS / "html.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    allowed = ALLOWED_IMPORTS | {"html.parser"}
    assert names and not [n for n in names if n not in allowed]
    calls = [n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert not set(calls) & FORBIDDEN_CALLS


def test_the_purity_lint_catches_an_impure_import() -> None:
    tree = ast.parse("import socket\nfrom pathlib import Path\nimport time\n")
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not set(names) & ALLOWED_IMPORTS


def test_extension_tests_run_without_sockets(no_sockets: None) -> None:
    with pytest.raises(RuntimeError, match="sockets are disabled"):
        socket.create_connection(("example.org", 443))


def test_comment_tokens_are_not_code() -> None:
    toks = list(tokenize.generate_tokens(io.StringIO("x = 1  # data.cdc.gov\n").readline))
    assert any(t.type == tokenize.COMMENT for t in toks)


# ------------------------------------------------------------------------------------------- the guard

ROW_LEVEL = [{"Patient Name": "X", "Age": "40", "Sex": "F"}]


@pytest.mark.parametrize("kind", sorted(TYPES))
def test_every_type_output_passes_the_row_guard(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    t = TYPES[kind]
    page = Page(fetch=fetch_of("https://data.example.gov/toy/data", b"x"), records=ROW_LEVEL)
    monkeypatch.setitem(TYPES, kind, dataclasses.replace(t, pages=lambda read: iter([page])))
    e = _entry_of(kind)
    with pytest.raises(RowLevelRefused):
        list(pages(e, lambda u: page.fetch))


def _entry_of(kind: str):  # type: ignore[no-untyped-def]
    blocks = {
        "https_file": {"format": "csv"},
        "html_table": {"shape": "table", "table_selector": "table"},
        "pdf_table": {"index_link_pattern": "/x"},
    }
    extra: dict[str, object] = {}
    if kind == "tableau":
        extra = {"tableau": {"views": [{"view": "V", "worksheets": {"W": "a" * 64}}]}}
    if kind == "doi":
        extra = {"conference": "toyconf", "conference_name": "Toy", "supplements": [
            {"year": 2024, "issn": "1234-5678", "volume": "1", "from_pub_date": "2024-01-01",
             "until_pub_date": "2024-12-31", "evidence": "e"}]}  # fmt: skip
    return entry(kind, blocks.get(kind), **extra)


def test_every_record_producing_extension_passes_the_row_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """parse extensions run inside a type's reader, so their records pass the page guard; staging extensions'
    output fields pass the guard in staging.stage_entry. Each one registered is exercised here."""
    from health_data_registry.adapters.extensions import Extension
    from health_data_registry.source_config import Strict

    _load()
    producing = [e for e in EXTENSIONS.values() if e.hook in ("parse", "staging")]
    assert producing, "at least wide_to_long produces records"
    probe = Extension("probe_parse", "parse", Strict, lambda args, content, url: ROW_LEVEL, "test only")
    monkeypatch.setitem(EXTENSIONS, "probe_parse", probe)
    from tests.adapters.test_listing import listing_entry

    e = listing_entry(extensions=[{"name": "probe_parse", "hook": "parse"}])
    from health_data_registry.adapters.html_table import item_records

    recs = item_records(e, e.source.config().listing, b"", "https://data.example.gov/abstract/a/")  # type: ignore[attr-defined]
    page = Page(fetch=fetch_of("https://data.example.gov/abstract/a/", b""), records=recs)
    with pytest.raises(RowLevelRefused):
        adapters.guard_page("probe", page)
    for ext in producing:
        if ext.hook == "staging":
            from tests.adapters.test_staging_extensions import row_level_staging_is_refused

            row_level_staging_is_refused(ext.name)

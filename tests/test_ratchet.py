"""The trusted gate's ratchet: each rule fails a change that weakens a check and passes one that does not.

Every marker the ratchet looks for is spelled here in two pieces ("# no" + "qa"), so this file trips none of them
itself (test_the_gate_files_trip_none_of_their_own_markers).
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
import yaml

from health_data_registry import ratchet as r
from health_data_registry.ratchet import Change, Finding, Snapshot

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "trusted-gate.yml"

SKIP = "@pytest.mark." + "skip"
SKIPIF = "@pytest.mark." + "skipif"
XFAIL = "@pytest.mark." + "xfail"
NO_COVER = "# pragma: " + "no cover"
NO_MUTATE = "# pragma: " + "no " + "mutate"
TYPE_IGNORE = "# type: " + "ignore"
NOQA = "# no" + "qa"
MYPY_INLINE = "# my" + "py: ignore-errors"
NO_VERIFY = "--no-" + "verify"
DESELECT = "--de" + "select"
HOOKS_PATH = "core." + "hooksPath"
OR_TRUE = "|| " + "true"
CI = ".github/workflows/ci.yml"


def check(
    base: dict[str, str],
    head: dict[str, str],
    *,
    allow: str | None = None,
    required: list[str] | None = None,
    changes: list[Change] | None = None,
) -> list[Finding]:
    trusted = dict(base)
    if allow is not None:
        trusted[r.ALLOWLIST] = allow
    return r.check(
        r.changes_between(base, head) if changes is None else changes,
        Snapshot.of(base),
        Snapshot.of(head),
        Snapshot.of(trusted),
        required,
    )


def one(findings: list[Finding]) -> Finding:
    assert len(findings) == 1, [f.render() for f in findings]
    return findings[0]


# Output ------------------------------------------------------------------------------------------------------


def test_clean_replaces_control_characters_and_caps_the_length() -> None:
    assert r.clean("a\nb\rc\x1bd\x7fe") == "a?b?c?d?e"
    assert r.clean("::error::x") == "::error::x"
    assert r.clean("x" * r.LINE_TEXT_LIMIT) == "x" * r.LINE_TEXT_LIMIT
    long = r.clean("y" * (r.LINE_TEXT_LIMIT + 1))
    assert len(long) == r.LINE_TEXT_LIMIT
    assert long.endswith("yy...")


def test_render_names_file_line_rule_and_the_allowlist_entry() -> None:
    f = Finding("escape hatch", "src/a.py", 7, "adds x", "y = 1", True)
    assert f.render() == (
        "FAIL [escape hatch] src/a.py:7: adds x\n"
        f"    If it is needed, add this exact line to {r.ALLOWLIST} in a pull request that changes nothing else:\n"
        "    src/a.py :: y = 1"
    )
    assert Finding("sealed sets", "eval/sealed/x", 0, "m").render() == "FAIL [sealed sets] eval/sealed/x: m"


def test_annotations_escape_what_could_end_or_forge_a_command() -> None:
    f = Finding("hooks", "a:b,c%.py", 3, "50% done\nnext\r")
    assert f.annotation() == "::error file=a%3Ab%2Cc%25.py,line=3,title=Trusted gate::50%25 done%0Anext%0D"
    assert Finding("x", "p", 0, "m").annotation() == "::error file=p,title=Trusted gate::m"


# Allowlist ---------------------------------------------------------------------------------------------------


def test_parse_allowlist_reads_entries_and_skips_comments_blanks_and_junk() -> None:
    text = (
        "# why\n\n  tests/a.py ::   x = 1  \nno separator\n :: nothing\npath :: \nb.py :: y :: z\n#c :: d\n"
    )
    assert r.parse_allowlist(text) == {("tests/a.py", "x = 1"), ("b.py", "y :: z")}
    assert r.parse_allowlist(None) == frozenset()


# Rule 5: sealed sets -----------------------------------------------------------------------------------------


def test_touching_the_sealed_sets_fails() -> None:
    f = one(check({"eval/sealed/a.json": "1"}, {"eval/sealed/a.json": "2"}))
    assert (f.rule, f.path, f.line, f.allowlistable) == ("sealed sets", "eval/sealed/a.json", 0, False)
    assert "eval/sealed/" in f.message


def test_moving_a_file_out_of_the_sealed_sets_fails_on_its_old_path() -> None:
    findings = check({}, {}, changes=[Change("eval/sealed/q.json", "eval/q.json")])
    assert [f.path for f in findings] == ["eval/sealed/q.json"]


def test_other_eval_files_are_not_sealed() -> None:
    assert check({"eval/dev/a.json": "1"}, {"eval/dev/a.json": "2"}) == []


# Rule 7: the allowlist and the gate change alone -------------------------------------------------------------


def test_the_allowlist_may_change_alone() -> None:
    assert check({r.ALLOWLIST: ""}, {r.ALLOWLIST: "tests/a.py :: x"}) == []


def test_the_allowlist_with_anything_else_fails() -> None:
    f = one(check({r.ALLOWLIST: "", "a.py": "1"}, {r.ALLOWLIST: "a.py :: x", "a.py": "2"}))
    assert (f.rule, f.path, f.allowlistable) == ("allowlist", r.ALLOWLIST, False)
    assert f.message.endswith("1 other file(s): a.py")


def test_the_allowlist_message_lists_at_most_five_other_files() -> None:
    head = {r.ALLOWLIST: "x"} | {f"f{i}.txt": "1" for i in range(7)}
    f = one(check({}, head))
    assert "7 other file(s): f0.txt, f1.txt, f2.txt, f3.txt, f4.txt" in f.message
    assert "f5.txt" not in f.message


@pytest.mark.parametrize("other", ["docs/dev/README.md", "CHANGELOG.md", "tests/test_ratchet.py"])
def test_the_gate_may_change_with_docs_and_its_own_files(other: str) -> None:
    head = {"src/health_data_registry/ratchet.py": "x = 1\n", other: "y\n"}
    assert check({}, head) == []


def test_the_gate_with_other_code_fails() -> None:
    head = {".github/workflows/trusted-gate.yml": "on: {}\n", "src/health_data_registry/query.py": "x = 1\n"}
    f = one(check({}, head))
    assert (f.rule, f.path) == ("gate files", ".github/workflows/trusted-gate.yml")
    assert "1 other file(s): src/health_data_registry/query.py" in f.message
    assert "(.github/workflows/trusted-gate.yml)" in f.message


# Rules 2 and 6: escape hatches line by line ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "line", "what"),
    [
        ("tests/helpers.py", SKIP, "skip"),
        ("tests/helpers.py", SKIPIF + "(True, reason='x')", "skip"),
        ("tests/helpers.py", XFAIL, "skip"),
        ("tests/helpers.py", "from pytest import mark\n" + "@mark." + "xfail", "skip"),
        ("tests/helpers.py", "    pytest." + "skip('later')", "skip"),
        ("tests/helpers.py", "x = pytest." + "importorskip('y')", "skip"),
        ("tests/helpers.py", "@unittest." + "skipIf(True, 'x')", "skip"),
        ("tests/helpers.py", "        self." + "skipTest('x')", "skip"),
        ("pyproject.toml", f'addopts = "{DESELECT} tests/test_a.py::t"', "deselection"),
        ("Makefile", "\tpytest --ig" + "nore=tests/x", "deselection"),
        ("tests/conftest.py", "collect_ig" + "nore = ['x.py']", "deselection"),
        ("src/a.py", "def f():  " + NO_COVER, "no-cover"),
        ("src/a.py", "x = 1  " + NO_MUTATE, "no-mutate"),
        ("src/a.py", "x: int = y  " + TYPE_IGNORE + "[assignment]", "type-ignore"),
        ("src/a.py", "import os  " + NOQA + ": F401", "noqa"),
        ("src/a.py", "# ruff: no" + "qa", "noqa"),
        ("src/a.py", MYPY_INLINE, "inline mypy"),
        ("Makefile", "\truff check --extend-" + "ignore E1 src", "relaxation"),
        (CI, "        run: mypy --disable-" + "error-code attr", "relaxation"),
        ("Makefile", "\truff check --exit" + "-zero src", "relaxation"),
        (CI, "        run: make test " + OR_TRUE, "failure is ignored"),
        ("Makefile", "\tmake test " + OR_TRUE, "failure is ignored"),
    ],
)
def test_each_new_escape_hatch_fails(path: str, line: str, what: str) -> None:
    f = one(check({path: "a = 1\n"}, {path: f"a = 1\n{line}\n"}))
    assert f.rule == "escape hatch"
    assert what in f.message
    assert f.path == path
    assert f.line == 1 + line.count("\n") + 1
    assert f.text == line.splitlines()[-1].strip()
    assert f.allowlistable


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("docs/a.md", SKIP),  # prose about a mark is not a mark
        ("src/a.json", NOQA),
        ("Makefile", "\tcmd  " + TYPE_IGNORE),
        ("src/a.py", "skip = True"),
        ("src/a.py", "# type of x"),
        ("src/a.py", "# pragma: cover me"),
        ("src/a.py", "uv run mypy  # mypy is strict"),
        ("Makefile", "\ta || b"),
    ],
)
def test_lookalikes_and_other_file_types_do_not_trip(path: str, line: str) -> None:
    assert check({path: ""}, {path: line + "\n"}) == []


def test_an_escape_hatch_already_there_or_moved_does_not_trip() -> None:
    base = {"src/a.py": f"x = 1  {NOQA}\ny = 2\n"}
    head = {"src/a.py": f"y = 2\nz = 3\nx = 1  {NOQA}\n"}
    assert check(base, head) == []


def test_a_second_copy_of_an_existing_line_trips() -> None:
    line = f"x = 1  {NOQA}"
    f = one(check({"src/a.py": line + "\n"}, {"src/a.py": f"{line}\ny = 2\n{line}\n"}))
    assert f.line == 3


def test_a_renamed_file_is_compared_with_its_old_name() -> None:
    line = f"x = 1  {TYPE_IGNORE}"
    base = {"src/old.py": line + "\n"}
    head = {"src/new.py": line + "\n"}
    assert check(base, head, changes=[Change("src/old.py", "src/new.py")]) == []
    assert len(check(base, head, changes=[Change(None, "src/new.py")])) == 1


def test_an_allowlisted_line_passes_and_only_in_its_own_file() -> None:
    line = f"import os  {NOQA}: F401"
    allow = f"src/a.py :: {line}\n"
    assert check({"src/a.py": ""}, {"src/a.py": line + "\n"}, allow=allow) == []
    assert len(check({"src/b.py": ""}, {"src/b.py": line + "\n"}, allow=allow)) == 1


def test_deleted_binary_and_allowlist_files_are_not_scanned() -> None:
    assert check({"src/a.py": NOQA}, {}) == []
    assert check({}, {"src/a.py": f"\0{NOQA}\n"}) == []
    assert check({r.ALLOWLIST: ""}, {r.ALLOWLIST: f"src/a.py :: x  {NOQA}\nMakefile :: {NO_VERIFY}\n"}) == []


@pytest.mark.parametrize(
    "line",
    [f"\tgit commit {NO_VERIFY}", "\tgit commit -am x -" + "n", f"\tgit config {HOOKS_PATH} /dev/null"],
)
def test_a_way_around_the_hooks_fails_and_cannot_be_allowlisted(line: str) -> None:
    f = one(check({"Makefile": ""}, {"Makefile": line + "\n"}, allow=f"Makefile :: {line.strip()}\n"))
    assert (f.rule, f.allowlistable, f.line) == ("hooks", False, 1)
    assert "git hooks" in f.message


def test_the_hook_bypass_in_prose_may_be_allowlisted() -> None:
    line = f"Never use `{NO_VERIFY}`."
    f = one(check({"CONTRIBUTING.md": ""}, {"CONTRIBUTING.md": line + "\n"}))
    assert (f.rule, f.allowlistable) == ("hooks", True)
    assert (
        check({"CONTRIBUTING.md": ""}, {"CONTRIBUTING.md": line + "\n"}, allow=f"CONTRIBUTING.md :: {line}")
        == []
    )


def test_the_gate_files_trip_none_of_their_own_markers() -> None:
    # Under mutmut the suite runs in mutants/, whose copy of the module is rewritten; read the checkout's own.
    source = ROOT.parent if ROOT.name == "mutants" else ROOT
    for rel in ("src/health_data_registry/ratchet.py", "tests/test_ratchet.py"):
        text = (source / rel).read_text(encoding="utf-8")
        assert check({rel: ""}, {rel: text}) == [], rel


# Rule 1: thresholds ------------------------------------------------------------------------------------------

MAKEFILE = "COV_FLOOR ?= 85\nMUTATION_FLOOR ?= 70\n"


def test_lowering_the_coverage_floor_fails() -> None:
    findings = check({"Makefile": MAKEFILE}, {"Makefile": MAKEFILE.replace("85", "80")})
    assert {(f.rule, f.path, f.line) for f in findings} == {("threshold", "Makefile", 1)}
    messages = sorted(f.message for f in findings)
    assert messages == [
        "lowers cov_floor from 85 to 80: COV_FLOOR ?= 80",
        "sets cov_floor to 80, below the project's floor of 85: COV_FLOOR ?= 80",
    ]
    assert not any(f.allowlistable for f in findings)


def test_raising_a_floor_passes() -> None:
    assert check({"Makefile": MAKEFILE}, {"Makefile": MAKEFILE.replace("85", "90").replace("70", "75")}) == []


def test_a_floor_below_the_projects_floor_fails_even_when_new() -> None:
    f = one(check({}, {".github/workflows/x.yml": "env:\n  MUTATION_FLOOR: '60'\n"}))
    assert f.line == 2
    assert f.message == "sets mutation_floor to 60, below the project's floor of 70: MUTATION_FLOOR: '60'"


@pytest.mark.parametrize(
    ("path", "before", "after", "name"),
    [
        ("scripts/precheck.sh", 'M="${MUTATION_FLOOR:-70}"', 'M="${MUTATION_FLOOR:-65}"', "mutation_floor"),
        ("scripts/m.py", 'ap.add_argument("--floor", type=float, default=70.0)', 'ap.add_argument("--floor", type=float, default=50)', "floor"),
        ("pyproject.toml", "[tool.coverage.report]\nfail_under = 85", "[tool.coverage.report]\nfail_under = 84.5", "fail_under"),
        ("Makefile", "\tpytest --cov-fail-under=85", "\tpytest --cov-fail-under 80", "cov_fail_under"),
        (CI, "  run: x --floor 70", "  run: x --floor=10", "floor"),
    ],
)  # fmt: skip
def test_each_spelling_of_a_floor_is_read(path: str, before: str, after: str, name: str) -> None:
    findings = check({path: before + "\n"}, {path: after + "\n"})
    assert any(f.message.startswith(f"lowers {name} from ") for f in findings), [f.render() for f in findings]
    assert all(f.rule == "threshold" and f.path == path for f in findings)


def test_lowering_another_threshold_fails_without_a_project_floor() -> None:
    f = one(check({"scripts/a.sh": "SIZE_THRESHOLD=10\n"}, {"scripts/a.sh": "SIZE_THRESHOLD=9.5\n"}))
    assert f.message == "lowers size_threshold from 10 to 9.5: SIZE_THRESHOLD=9.5"


def test_the_lowest_of_several_settings_is_compared() -> None:
    base = {"Makefile": "FOO_FLOOR = 50\nFOO_FLOOR = 60\n"}
    assert check(base, {"Makefile": "FOO_FLOOR = 55\nFOO_FLOOR = 50\n"}) == []
    f = one(check(base, {"Makefile": "FOO_FLOOR = 60\nFOO_FLOOR = 49\n"}))
    assert (f.line, f.message) == (2, "lowers foo_floor from 50 to 49: FOO_FLOOR = 49")


def test_removing_a_threshold_fails() -> None:
    f = one(check({"Makefile": "x\nFOO_FLOOR = 50\n"}, {"Makefile": "x\n"}))
    assert (f.line, f.message) == (0, "removes the threshold foo_floor (was 50 at line 2: FOO_FLOOR = 50)")


def test_a_threshold_turned_into_a_variable_or_negative_counts_as_removed() -> None:
    f = one(check({"Makefile": "FOO_FLOOR = 50\n"}, {"Makefile": "FOO_FLOOR = -1\n"}))
    assert f.message.startswith("removes the threshold foo_floor")


def test_deleting_a_file_with_a_threshold_fails_at_its_old_line() -> None:
    f = one(check({"scripts/a.sh": "#!/bin/sh\nBAR_THRESHOLD=3\n"}, {}))
    assert (f.path, f.line) == ("scripts/a.sh", 2)


def test_a_moved_threshold_file_is_compared_with_its_old_name() -> None:
    changes = [Change("scripts/a.sh", "scripts/b.sh")]
    assert check({"scripts/a.sh": "X_FLOOR=3\n"}, {"scripts/b.sh": "X_FLOOR=3\n"}, changes=changes) == []


def test_thresholds_outside_the_tool_configs_are_not_read() -> None:
    assert check({"src/a.py": "SCORE_FLOOR = 9\n"}, {"src/a.py": "SCORE_FLOOR = 1\n"}) == []
    assert check({"docs/x.md": "COV_FLOOR ?= 85\n"}, {"docs/x.md": "COV_FLOOR ?= 1\n"}) == []


@pytest.mark.parametrize(
    ("name", "floor"),
    [
        ("cov_floor", 85.0), ("fail_under", 85.0), ("cov_fail_under", 85.0), ("mutation_floor", 70.0),
        ("floor", 70.0), ("mutation_fail_under", 70.0), ("size_threshold", None), ("x_floor", None),
    ],
)  # fmt: skip
def test_minimum_for(name: str, floor: float | None) -> None:
    assert r.minimum_for(name) == floor


@pytest.mark.parametrize(
    ("path", "scoped"),
    [
        ("Makefile", True), ("pyproject.toml", True), (".pre-commit-config.yaml", True), ("setup.cfg", True),
        ("tox.ini", True), ("pytest.ini", True), (".coveragerc", True), ("mypy.ini", True), ("ruff.toml", True),
        (".ruff.toml", True), ("scripts/x.py", True), (CI, True), (".github/actions/setup/action.yml", True),
        ("src/Makefile", False), ("docs/pyproject.toml", False), ("tests/scripts/x.py", False),
    ],
)  # fmt: skip
def test_threshold_scope(path: str, scoped: bool) -> None:
    assert r.in_threshold_scope(path) is scoped


# Rule 2: TOML relaxations ------------------------------------------------------------------------------------

PYPROJECT = """\
[tool.ruff.lint]
select = ["E", "F", "S"]
ignore = ["E501"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]

[tool.mypy]
files = ["src", "tools"]
strict = true
warn_unused_ignores = true

[tool.coverage.run]
source = ["pkg"]

[tool.mutmut]
source_paths = ["src/pkg/"]
"""


def toml_findings(
    new: str, path: str = "pyproject.toml", old: str = PYPROJECT, allow: str = ""
) -> list[Finding]:
    return [f for f in check({path: old}, {path: new}, allow=allow) if f.rule == "escape hatch"]


@pytest.mark.parametrize(
    ("old", "new", "message", "line"),
    [
        ('select = ["E", "F", "S"]', 'select = ["E", "F"]', "ruff select no longer has S", 2),
        ('ignore = ["E501"]', 'ignore = ["E501", "B008"]', "ruff ignore gains B008", 3),
        ('"tests/**" = ["S101"]', '"tests/**" = ["S101", "S608"]', "ruff per-file-ignores gains tests/**: S608", 6),
        ("strict = true", "strict = false", "mypy strict turned off", 10),
        ("strict = true\n", "", "mypy strict removed", 0),
        ("warn_unused_ignores = true", "warn_unused_ignores = false", "mypy warn_unused_ignores turned off", 11),
        ("strict = true", "strict = true\nignore_errors = true", "mypy ignore_errors turned on", 11),
        ("strict = true", 'strict = true\nfollow_imports = "skip"', "mypy follow_imports = skip", 11),
        ("strict = true", 'strict = true\ndisable_error_code = ["attr-defined"]', "mypy disable_error_code gains attr-defined", 11),
        ('files = ["src", "tools"]', 'files = ["src"]', "mypy files no longer has tools", 9),
        ("strict = true", 'strict = true\nexclude = ["^src/x"]', "mypy exclude gains ^src/x", 11),
        ('source = ["pkg"]', 'source = []', "coverage run.source no longer has pkg", 14),
        ('source = ["pkg"]', 'source = ["pkg"]\nomit = ["src/x.py"]', "coverage run.omit gains src/x.py", 15),
        ('source_paths = ["src/pkg/"]', 'source_paths = []', "mutmut source_paths no longer has src/pkg/", 17),
        ('source_paths = ["src/pkg/"]', 'source_paths = ["src/pkg/"]\ndo_not_mutate = ["src/pkg/x.py"]', "mutmut do_not_mutate gains src/pkg/x.py (a mutmut exclusion)", 18),
    ],
)  # fmt: skip
def test_each_toml_relaxation_fails(old: str, new: str, message: str, line: int) -> None:
    assert old in PYPROJECT
    f = one(toml_findings(PYPROJECT.replace(old, new)))
    assert f.message == f"relaxes a check: {message}"
    assert f.line == line
    assert f.allowlistable is (line > 0)


def test_mypy_overrides_coverage_report_and_mutmut_arguments() -> None:
    extra = """
[[tool.mypy.overrides]]
module = ["pkg.x"]
ignore_errors = true
disallow_untyped_defs = false

[tool.coverage.report]
exclude_lines = ["raise NotImplementedError"]
exclude_also = ["if TYPE_CHECKING:"]
omit = ["src/y.py"]
"""
    new = PYPROJECT + 'pytest_add_cli_args = ["-x"]\n' + extra
    messages = sorted(f.message.removeprefix("relaxes a check: ") for f in toml_findings(new))
    assert messages == [
        "coverage report.exclude_also gains if TYPE_CHECKING:",
        "coverage report.exclude_lines gains raise NotImplementedError",
        "coverage report.omit gains src/y.py",
        "mutmut pytest_add_cli_args gains -x (a mutmut exclusion)",
        "mypy override for pkg.x: disallow_untyped_defs turned off",
        "mypy override for pkg.x: ignore_errors turned on",
    ]


def test_an_unchanged_override_is_not_reported_again() -> None:
    override = '\n[[tool.mypy.overrides]]\nmodule = "pkg.x"\nignore_errors = true\n'
    assert toml_findings(PYPROJECT + override + "\n", old=PYPROJECT + override) == []
    f = one(toml_findings(PYPROJECT + override.replace("pkg.x", "pkg.y"), old=PYPROJECT + override))
    assert f.message == "relaxes a check: mypy override for pkg.y: ignore_errors turned on"


def test_tightening_the_toml_passes() -> None:
    new = PYPROJECT.replace('ignore = ["E501"]', "ignore = []").replace(
        'select = ["E", "F", "S"]', 'select = ["E", "F", "S", "B"]'
    )
    new += '\n[tool.mypy.overrides]\nmodule = "x"\nwarn_return_any = true\n'
    assert toml_findings(new.replace("[tool.mypy.overrides]", "[[tool.mypy.overrides]]")) == []


def test_a_per_file_ignore_is_located_on_its_own_line() -> None:
    old = PYPROJECT.replace('"tests/**" = ["S101"]', '"tests/**" = ["S101"]\n"scripts/**" = ["S603"]')
    new = old.replace('"scripts/**" = ["S603"]', '"scripts/**" = ["S603"]\n"src/x.py" = [\n  "S603",\n]')
    f = one(toml_findings(new, old=old))
    assert (f.line, f.text) == (8, '"src/x.py" = [')


def test_locate() -> None:
    text = "a b\nc d\nb c\n"
    assert r._locate(text, "c\0b") == (3, "b c")
    assert r._locate(text, "c\0zz") == (2, "c d")
    assert r._locate(text, "zz\0c") == (0, "")
    assert r._locate(None, "a") == (0, "")


def test_a_ruff_toml_is_read_at_its_top_level() -> None:
    f = one(
        toml_findings(
            'ignore = ["E501"]\n[lint]\nextend-ignore = ["S1"]\n', "ruff.toml", 'ignore = ["E501"]\n'
        )
    )
    assert (f.path, f.line, f.message) == ("ruff.toml", 3, "relaxes a check: ruff extend-ignore gains S1")


def test_a_toml_relaxation_may_be_allowlisted_by_its_line() -> None:
    new = PYPROJECT.replace('ignore = ["E501"]', 'ignore = ["E501", "B008"]')
    assert toml_findings(new, allow='pyproject.toml :: ignore = ["E501", "B008"]') == []


def test_an_unreadable_toml_fails() -> None:
    f = one(toml_findings("[tool.ruff\n"))
    assert (f.path, f.line, f.allowlistable) == ("pyproject.toml", 0, False)
    assert f.message.startswith("could not read it to check it (")


def test_an_unreadable_merge_base_toml_is_compared_as_empty() -> None:
    f = one(toml_findings('[tool.ruff.lint]\nignore = ["E1"]\n', old="[broken\n"))
    assert f.message == "relaxes a check: ruff ignore gains E1"


def test_a_huge_toml_is_not_parsed_and_fails() -> None:
    f = one(toml_findings("#" * (r.MAX_PARSE_BYTES + 1)))
    assert "too large to check" in f.message


def test_toml_items_helpers() -> None:
    assert r._items("a") == ["a"]
    assert r._items(["a", 1, {"b": 2}]) == ["a", "1", '{"b": 2}']
    assert r._items(3) == []
    assert r._pairs({"t/*": ["A", "B"], "u": "C"}) == {"t/*: A", "t/*: B", "u: C"}
    assert r._pairs(["x"]) == set()
    assert r._table({"a": {"b": {"c": 1}}}, "a", "b") == {"c": 1}
    assert r._table({"a": 1}, "a", "b") == {}


# Rule 3: tests -----------------------------------------------------------------------------------------------

TESTS = """\
import pytest


def test_a():
    pass


@pytest.mark.parametrize("x", [1, 2, 3])
def test_b(x):
    pass


class TestC:
    def test_d(self):
        pass

    def helper(self):
        pass


def helper():
    pass
"""


def test_collect_tests_counts_functions_methods_and_parameters() -> None:
    assert r.collect_tests(TESTS) == {"test_a": (1, 4), "test_b": (3, 9), "TestC::test_d": (1, 14)}


def test_collect_tests_multiplies_class_and_stacked_parameters_and_reads_testcase() -> None:
    source = """
import unittest, pytest

@pytest.mark.parametrize("a", (1, 2))
class TestK:
    @pytest.mark.parametrize("b", [1, 2, 3])
    @pytest.mark.parametrize(argnames="c", argvalues=[1, 2])
    async def test_m(self, a, b, c): ...
    class TestInner:
        def test_n(self): ...

class Checks(unittest.TestCase):
    def test_o(self): ...

class Other:
    def test_p(self): ...

@pytest.mark.parametrize("d", CASES)
def test_q(d): ...

@pytest.mark.parametrize("e", [])
def test_r(e): ...

@other.parametrize
def test_s(): ...
"""
    found = r.collect_tests(source)
    assert found["TestK::test_m"][0] == 12
    assert found["TestK::TestInner::test_n"][0] == 2
    assert found["Checks::test_o"][0] == 1
    assert "Other::test_p" not in found
    assert found["test_q"][0] == 1
    assert found["test_r"][0] == 1
    assert found["test_s"][0] == 1


def test_deleting_a_test_fails_naming_it() -> None:
    head = TESTS.replace("def test_a():\n    pass\n", "")
    f = one(check({"tests/test_x.py": TESTS}, {"tests/test_x.py": head}))
    assert (f.rule, f.path, f.line) == ("tests", "tests/test_x.py", 4)
    assert f.message == (
        "the test modules it touches had 5 tests at the merge base and have 4 now; removed or renamed without a "
        "replacement: test_a (tests/test_x.py line 4 at the merge base)"
    )


def test_shrinking_a_parametrize_list_fails() -> None:
    f = one(check({"tests/test_x.py": TESTS}, {"tests/test_x.py": TESTS.replace("[1, 2, 3]", "[1, 2]")}))
    assert "had 5 tests at the merge base and have 4 now" in f.message
    assert f.message.endswith("a parametrized case")
    assert (f.path, f.line) == ("tests/", 0)


def test_renaming_or_moving_a_test_passes() -> None:
    assert check({"tests/test_x.py": TESTS}, {"tests/test_x.py": TESTS.replace("test_a", "test_a2")}) == []
    moved = TESTS.replace("def test_a():\n    pass\n", "")
    head = {"tests/test_x.py": moved, "tests/unit/test_y.py": "def test_a():\n    pass\n"}
    assert check({"tests/test_x.py": TESTS}, head) == []


def test_deleting_a_test_module_fails() -> None:
    f = one(check({"tests/test_x.py": TESTS}, {}))
    assert "have 0 now" in f.message
    assert "test_a (tests/test_x.py line 4" in f.message


def test_a_renamed_module_is_compared_with_its_old_name() -> None:
    changes = [Change("tests/test_x.py", "tests/test_z.py")]
    assert check({"tests/test_x.py": TESTS}, {"tests/test_z.py": TESTS}, changes=changes) == []


def test_an_allowlisted_removal_passes() -> None:
    head = TESTS.replace("def test_a():\n    pass\n", "")
    allow = "tests/test_x.py :: def test_a\n"
    assert check({"tests/test_x.py": TESTS}, {"tests/test_x.py": head}, allow=allow) == []
    head2 = head.replace("    def test_d(self):\n        pass\n", "")
    f = one(check({"tests/test_x.py": TESTS}, {"tests/test_x.py": head2}, allow=allow))
    assert "have 3 now" in f.message
    assert "TestC::test_d" in f.message
    assert "test_a (" not in f.message


def test_a_head_that_does_not_parse_fails() -> None:
    f = one(check({"tests/test_x.py": TESTS}, {"tests/test_x.py": "def test_a(:\n"}))
    assert (f.rule, f.line) == ("tests", 1)
    assert f.message.startswith("could not count its tests (head): ")


def test_a_merge_base_that_does_not_parse_counts_as_empty() -> None:
    assert check({"tests/test_x.py": "def (:\n"}, {"tests/test_x.py": TESTS}) == []


def test_a_huge_test_module_fails() -> None:
    f = one(check({}, {"tests/test_x.py": "#" * (r.MAX_PARSE_BYTES + 1)}))
    assert f.message == "too large to count its tests (head)"


def test_the_message_lists_at_most_ten_removed_tests() -> None:
    base = "".join(f"def test_{i}():\n    pass\n" for i in range(12))
    f = one(check({"tests/test_x.py": base}, {"tests/test_x.py": "x = 1\n"}))
    assert "test_9 (" in f.message
    assert "test_10 (" not in f.message


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("tests/test_a.py", True), ("tests/unit/a_test.py", True), ("tests/conftest.py", False),
        ("tests/test_a.txt", False), ("src/test_a.py", False), ("tests/helpers.py", False),
    ],
)  # fmt: skip
def test_is_test_module(path: str, is_test: bool) -> None:
    assert r.is_test_module(path) is is_test


# Rule 4: required checks -------------------------------------------------------------------------------------

CI_YML = """\
name: CI
on: [pull_request]
jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - run: make lint
  test:
    name: Test (Python ${{ matrix.python }})
    strategy:
      matrix:
        python: ["3.12", "3.13"]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - run: make test
  result:
    name: CI result
    if: always()
    needs: [lint, test]
    runs-on: ubuntu-latest
    steps:
      - run: echo done
"""
REQUIRED = ["CI result", "Test (Python 3.12)", "Analyze (python)"]


def required_findings(
    new: str, old: str = CI_YML, allow: str = "", required: list[str] | None = None
) -> list[Finding]:
    found = check({CI: old}, {CI: new}, allow=allow, required=REQUIRED if required is None else required)
    return [f for f in found if f.rule == "required check"]


def test_an_unchanged_or_unrelated_change_passes() -> None:
    assert required_findings(CI_YML) == []
    assert required_findings(CI_YML.replace("make lint", "make lint lint2")) == []


def test_removing_or_renaming_a_required_check_fails() -> None:
    f = one(required_findings(CI_YML.replace("name: CI result", "name: CI summary")))
    assert (f.path, f.line, f.allowlistable) == (CI, 18, False)
    assert f.message.startswith(
        "removes or renames the required check 'CI result' (job 'result' at the merge base)"
    )
    f = one(required_findings(CI_YML.replace('"3.12", ', "")))
    assert "'Test (Python 3.12)'" in f.message
    assert f.line == 9


def test_a_second_job_reporting_a_required_check_fails() -> None:
    other = ".github/workflows/other.yml"
    impostor = (
        "jobs:\n  fake:\n    name: CI result\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n"
    )
    found = [f for f in check({CI: CI_YML}, {CI: CI_YML, other: impostor}, required=["CI result"])]
    f = one(found)
    assert (f.rule, f.path, f.line, f.allowlistable) == ("required check", other, 2, False)
    assert f.message.startswith("adds job 'fake', which reports the required check 'CI result'")


def test_moving_a_required_check_to_another_workflow_fails() -> None:
    other = ".github/workflows/other.yml"
    head = {
        CI: CI_YML.replace("name: CI result", "name: Other"),
        other: CI_YML.replace("name: Lint", "name: L2"),
    }
    found = [f for f in check({CI: CI_YML}, head, required=["CI result"]) if f.rule == "required check"]
    assert [f.path for f in found] == [other]


def test_a_new_required_check_may_be_added() -> None:
    assert required_findings(CI_YML, old="jobs: {}\n", required=["CI result"]) == []


def test_making_a_required_job_conditional_fails_and_may_be_allowlisted() -> None:
    new = CI_YML.replace("    if: always()\n", "    if: github.event_name == 'push'\n")
    f = one(required_findings(new))
    assert (f.line, f.allowlistable, f.text) == (20, True, "if: github.event_name == 'push'")
    assert f.message == (
        "makes job 'result', which a required check runs or waits for, conditional: if: was 'always()' at the "
        "merge base and is 'github.event_name == 'push'' now"
    )
    assert required_findings(new, allow=f"{CI} :: if: github.event_name == 'push'") == []


def test_removing_always_from_the_aggregate_fails() -> None:
    f = one(required_findings(CI_YML.replace("    if: always()\n", "")))
    assert "is '(none)' now" in f.message
    assert f.text == "if:"
    assert f.line == 18


def test_a_condition_on_a_job_the_aggregate_needs_fails() -> None:
    new = CI_YML.replace("  lint:\n    name: Lint\n", "  lint:\n    name: Lint\n    if: false\n")
    f = one(required_findings(new))
    assert "job 'lint'" in f.message
    assert "is 'false' now" in f.message
    assert f.line == 6


def test_a_condition_on_a_job_nothing_required_needs_passes() -> None:
    new = (
        CI_YML
        + "  docs:\n    name: Docs\n    if: false\n    runs-on: ubuntu-latest\n    steps:\n      - run: x\n"
    )
    assert required_findings(new) == []


def test_continue_on_error_on_a_required_job_fails_and_cannot_be_allowlisted() -> None:
    new = CI_YML.replace(
        "    runs-on: ubuntu-latest\n    steps:\n      - run: make lint",
        "    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n      - run: make lint",
    )
    f = one(required_findings(new, allow=f"{CI} :: continue-on-error: true"))
    assert (f.line, f.allowlistable) == (7, False)
    assert f.message == "adds continue-on-error to job 'lint', which a required check runs or waits for"


@pytest.mark.parametrize("value", ["false", "${{ false }}"])
def test_continue_on_error_false_passes(value: str) -> None:
    new = CI_YML.replace(
        "      - run: make lint", f"      - run: make lint\n        continue-on-error: {value}"
    )
    if value == "false":
        assert required_findings(new) == []
    else:
        assert len(required_findings(new)) == 1


def test_a_condition_on_a_required_step_fails_and_may_be_allowlisted() -> None:
    new = CI_YML.replace(
        "      - run: make test", "      - run: make test\n        if: github.event_name == 'x'"
    )
    f = one(required_findings(new))
    assert (f.line, f.allowlistable, f.text) == (18, True, "if: github.event_name == 'x'")
    assert f.message == (
        "adds if on a step of job 'test', which a required check runs or waits for: if: github.event_name == 'x'"
    )
    assert required_findings(new, allow=f"{CI} :: if: github.event_name == 'x'") == []


def test_continue_on_error_on_a_required_step_fails() -> None:
    new = CI_YML.replace("      - run: make test", "      - run: make test\n        continue-on-error: true")
    f = one(required_findings(new, allow=f"{CI} :: continue-on-error: true"))
    assert (f.line, f.allowlistable) == (18, False)
    assert "continue-on-error: true" in f.message


def test_an_existing_step_condition_moved_passes() -> None:
    old = CI_YML.replace("      - run: make test", "      - run: make test\n        if: x")
    new = CI_YML.replace(
        "      - uses: actions/checkout@v5", "      - uses: actions/checkout@v5\n        if: x"
    )
    assert required_findings(new, old=old) == []
    twice = old.replace(
        "      - uses: actions/checkout@v5", "      - uses: actions/checkout@v5\n        if: x"
    )
    assert len(required_findings(twice, old=old)) == 1


def test_dropping_a_needed_job_fails() -> None:
    f = one(required_findings(CI_YML.replace("needs: [lint, test]", "needs: [test]")))
    assert f.message == "the required check 'CI result' no longer runs or waits for job 'lint'"
    assert f.line == 21


def test_needs_as_a_single_string_and_transitive_needs() -> None:
    old = CI_YML.replace("needs: [lint, test]", "needs: test").replace(
        "    strategy:", "    needs: lint\n    strategy:"
    )
    assert required_findings(old, old=old) == []
    f = one(required_findings(old.replace("    needs: lint\n", ""), old=old, required=["CI result"]))
    assert "no longer runs or waits for job 'lint'" in f.message


def test_required_checks_not_produced_at_the_merge_base_are_ignored() -> None:
    assert required_findings(CI_YML, old="jobs: {}\n", required=["CI result"]) == []


def test_without_a_required_list_the_rule_is_skipped() -> None:
    assert check({CI: CI_YML}, {CI: ""}, required=None) == []


def test_check_names_fill_the_matrix() -> None:
    job = {
        "name": "T (${{ matrix.os }}, ${{ matrix.py }}, ${{ matrix.unknown }})",
        "strategy": {
            "matrix": {
                "os": ["linux", "mac"], "py": [3.12, True],
                "exclude": [{"os": "mac", "py": True}],
                "include": [{"os": "linux", "extra": 1}, {"os": "win", "py": "3.12"}, "junk"],
            }
        },
    }  # fmt: skip
    assert r.check_names("t", job) == [
        "T (linux, 3.12, ${{ matrix.unknown }})",
        "T (linux, true, ${{ matrix.unknown }})",
        "T (mac, 3.12, ${{ matrix.unknown }})",
        "T (win, 3.12, ${{ matrix.unknown }})",
    ]
    assert r.matrix_combos(job)[0] == {"os": "linux", "py": "3.12", "extra": "1"}
    assert r.check_names("lint", {}) == ["lint"]
    assert r.check_names("x", {"name": "A ${{ matrix.v }}"}) == ["A ${{ matrix.v }}"]
    assert r.check_names(
        "x", {"name": "A ${{ matrix.v }}", "strategy": {"matrix": "${{ fromJSON(x) }}"}}
    ) == ["A ${{ matrix.v }}"]
    assert r.check_names(
        "x", {"name": "A ${{ matrix.v }}", "strategy": {"matrix": {"include": [{"v": 1}, {"v": 1}]}}}
    ) == ["A 1"]


def test_workflows_reads_only_top_level_workflow_files() -> None:
    files = {
        CI: CI_YML,
        ".github/workflows/a.yaml": "jobs:\n  x: {}\n",
        ".github/workflows/sub/b.yml": CI_YML,
        ".github/workflows/c.txt": CI_YML,
        ".github/workflows/bad.yml": "jobs: [\n",
        ".github/workflows/list.yml": "- 1\n",
        ".github/workflows/nojobs.yml": "jobs: 3\n",
        ".github/workflows/big.yml": "#" * (r.MAX_PARSE_BYTES + 1),
    }
    flows = r.workflows(Snapshot.of(files))
    assert [w.path for w in flows] == [".github/workflows/a.yaml", CI]
    assert list(flows[1].jobs) == ["lint", "test", "result"]


def test_closure_ignores_unknown_and_repeated_needs() -> None:
    wf = r.Workflow(CI, "", {"a": {"needs": ["b", "c", "zz"]}, "b": {"needs": "c"}, "c": {"needs": ["a"]}})
    assert r.closure(wf, "a") == ["a", "b", "c"]
    assert r.closure(wf, "missing") == []


def test_job_line_finds_jobs_keys_and_steps() -> None:
    text = (
        "jobs:\n  a:\n    if: x\n    steps:\n      - if: y\n        run: z\n      - if: w\n  b:\n    run: 1\n"
    )
    assert r._job_line(text, "a") == 2
    assert r._job_line(text, "a", "if") == 3
    assert r._job_line(text, "a", "if", "w", step=True) == 7
    assert r._job_line(text, "a", "if", "nothing", step=True) == 5
    assert r._job_line(text, "a", "needs") == 2
    assert r._job_line(text, "b", "if") == 8
    assert r._job_line(text, "zz") == 0
    assert r._job_line("jobs:\n  a:\n", "a", "if") == 2
    assert r._job_line("jobs:\n  a:\n    # c\n  # d\n    if: x\n", "a", "if") == 5


@pytest.mark.parametrize(
    ("value", "truthy"),
    [(None, False), (False, False), ("false", False), (" False ", False), (True, True), ("${{ x }}", True)],
)
def test_truthy(value: object, truthy: bool) -> None:
    assert r._truthy(value) is truthy


# Running it --------------------------------------------------------------------------------------------------


def test_findings_are_deduplicated_across_required_checks() -> None:
    new = CI_YML.replace("      - run: make test", "      - run: make test\n        continue-on-error: true")
    found = required_findings(new, required=["CI result", "Test (Python 3.12)", "Test (Python 3.13)"])
    assert len(found) == 1


def test_parse_name_status() -> None:
    raw = "M\0a.py\0A\0b.py\0D\0c.py\0R100\0d.py\0e.py\0C075\0f.py\0g.py\0T\0h\0"
    assert r.parse_name_status(raw) == [
        Change("a.py", "a.py"), Change(None, "b.py"), Change("c.py", None), Change("d.py", "e.py"),
        Change(None, "g.py"), Change("h", "h"),
    ]  # fmt: skip
    assert r.parse_name_status("") == []


def test_changes_between() -> None:
    assert r.changes_between({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "x", "d": "4"}) == [
        Change("b", "b"),
        Change("c", None),
        Change(None, "d"),
    ]


def git(repo: Path, *args: str) -> str:
    """git in a throwaway repository, through the ratchet's own runner, with a fixed identity and no signing."""
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false")
    return r._git(repo, *identity, *args).decode().strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "Makefile").write_text(MAKEFILE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(TESTS)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / CI).write_text(CI_YML)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "pr")
    return tmp_path


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def run_main(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = r.main(list(args), out)
    return code, out.getvalue()


def test_main_passes_a_harmless_change(repo: Path, tmp_path: Path) -> None:
    (repo / "README.md").write_text("hello\n")
    head = commit(repo, "readme")
    required = tmp_path / "required.txt"
    required.write_text("CI result\n\n  Lint  \n")
    code, out = run_main(
        "--repo", str(repo), "--base", "main", "--head", head, "--required-checks", str(required)
    )
    assert code == 0
    assert out == "The trusted gate found nothing that weakens main's checks.\n"


def test_main_reports_each_finding_with_annotations(repo: Path) -> None:
    (repo / "Makefile").write_text(MAKEFILE.replace("85", "90"))
    (repo / "src.py").write_text(f"x = 1  {NOQA}\n")
    (repo / "tests" / "test_x.py").rename(repo / "tests" / "test_y.py")
    commit(repo, "weaken")
    code, out = run_main("--repo", str(repo), "--base", "main", "--head", "HEAD", "--github")
    assert code == 1
    lines = out.splitlines()
    assert lines[0] == "Required checks: not checked (no --required-checks file given)."
    assert f"FAIL [escape hatch] src.py:1: adds a noqa comment: x = 1  {NOQA}" in lines
    assert f"::error file=src.py,line=1,title=Trusted gate::adds a noqa comment: x = 1  {NOQA}" in lines
    assert lines[-1] == "The trusted gate found 1 way(s) this change weakens main's checks."


def test_main_compares_with_the_merge_base_not_mains_tip(repo: Path) -> None:
    git(repo, "checkout", "-q", "main")
    (repo / "Makefile").write_text(MAKEFILE.replace("85", "95"))
    commit(repo, "main raises the floor")
    git(repo, "checkout", "-q", "pr")
    (repo / "x.txt").write_text("x\n")
    head = commit(repo, "unrelated")
    assert run_main("--repo", str(repo), "--base", "main", "--head", head)[0] == 0


def test_main_reads_the_allowlist_from_main(repo: Path) -> None:
    line = f"x = 1  {NOQA}"
    git(repo, "checkout", "-q", "main")
    (repo / ".github" / "ratchet-allowlist.txt").write_text(f"src.py :: {line}\n")
    commit(repo, "allow")
    git(repo, "checkout", "-q", "pr")
    (repo / "src.py").write_text(line + "\n")
    head = commit(repo, "use it")
    assert run_main("--repo", str(repo), "--base", "main", "--head", head)[0] == 0
    assert run_main("--repo", str(repo), "--base", "main~1", "--head", head)[0] == 1


def test_main_fails_closed_when_git_cannot_answer(repo: Path, tmp_path: Path) -> None:
    code, out = run_main("--repo", str(repo), "--base", "main", "--head", "no-such-ref")
    assert code == 2
    assert out.startswith("The trusted gate could not check this change, so it fails: git rev-parse failed: ")
    code, out = run_main(
        "--repo",
        str(repo),
        "--base",
        "main",
        "--head",
        "HEAD",
        "--required-checks",
        str(tmp_path / "missing"),
    )
    assert code == 2
    code, out = run_main("--repo", str(tmp_path / "nowhere"), "--base", "main", "--head", "HEAD")
    assert code == 2


def test_main_fails_closed_on_unrelated_histories(repo: Path) -> None:
    git(repo, "checkout", "-q", "--orphan", "other")
    (repo / "a.txt").write_text("a\n")
    head = commit(repo, "orphan")
    code, out = run_main("--repo", str(repo), "--base", "main", "--head", head)
    assert code == 2
    assert "git merge-base failed" in out


def test_git_snapshot_reads_files_and_lists_paths(repo: Path) -> None:
    snap = r.git_snapshot(repo, git(repo, "rev-parse", "HEAD"))
    assert snap.read("Makefile") == MAKEFILE
    assert snap.read("missing.txt") is None
    assert snap.paths == [CI, "Makefile", "tests/test_x.py"]


def test_git_changes_detects_renames(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_x.py").rename(repo / "tests" / "test_z.py")
    (repo / "Makefile").unlink()
    (repo / "new.txt").write_text("n\n")
    (repo / "caf\udce9.txt").write_text("latin-1 name\n")
    head = commit(repo, "move")
    # Rename detection must come from -M, not the repository's config; and the commits, not the worktree, count.
    git(repo, "config", "diff.renames", "false")
    git(repo, "checkout", "-q", base)
    assert r.git_changes(repo, base, head) == [
        Change("Makefile", None),
        Change(None, "caf�.txt"),
        Change(None, "new.txt"),
        Change("tests/test_x.py", "tests/test_z.py"),
    ]


def test_commit_refuses_what_is_not_a_commit(repo: Path) -> None:
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    with pytest.raises(r.GitError):
        r._commit(repo, tree)
    with pytest.raises(r.GitError, match="git rev-parse failed"):
        r._commit(repo, "--output=x")


def test_git_ignores_hook_variables(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    monkeypatch.setenv("PATH_KEPT", "1")
    assert "PATH_KEPT" in r.git_env()
    assert not any(k.startswith("GIT_") for k in r.git_env())
    assert r.git_snapshot(repo, "HEAD").read("Makefile") == MAKEFILE


def test_read_required() -> None:
    assert r.read_required(None) is None


# The workflow ------------------------------------------------------------------------------------------------


def test_the_workflow_runs_mains_copy_read_only_with_no_secret() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    assert set(wf[True]) == {"pull_request_target", "merge_group"}
    assert wf[True]["pull_request_target"]["branches"] == ["main"]
    assert wf["permissions"] == {"contents": "read"}
    (job,) = wf["jobs"].values()
    assert job["name"] == "Trusted gate"
    assert job["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    for unsafe in (
        "head.ref",
        "head_ref",
        ".title",
        ".body",
        "head.repo",
        "github.event.pull_request.head.sha }}\n",
    ):
        assert unsafe not in text, unsafe
    checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["persist-credentials"] is False
    assert "head" not in checkouts[0]["with"]["ref"]
    setup = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("astral-sh/setup-uv@"))
    assert setup["with"]["enable-cache"] is False
    for step in job["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    run = "\n".join(s.get("run", "") for s in job["steps"])
    assert "${{" not in run
    assert "health_data_registry.ratchet" in run
    assert "--required-checks" in run
    assert "--locked" in run


def test_the_allowlist_starts_empty() -> None:
    assert r.parse_allowlist((ROOT / r.ALLOWLIST).read_text(encoding="utf-8")) == frozenset()


def test_a_new_pyproject_is_compared_with_nothing() -> None:
    found = check({}, {"pyproject.toml": '[tool.ruff.lint]\nignore = ["E1"]\n'})
    f = one([x for x in found if x.rule == "escape hatch"])
    assert (f.line, f.message) == (2, "relaxes a check: ruff ignore gains E1")


def test_dropping_an_enabled_mypy_error_code_fails() -> None:
    old = PYPROJECT.replace("strict = true", 'strict = true\nenable_error_code = ["ignore-without-code"]')
    f = one(toml_findings(PYPROJECT, old=old))
    assert f.message == "relaxes a check: mypy enable_error_code no longer has ignore-without-code"


def test_a_test_module_missing_on_one_side_counts_as_empty() -> None:
    assert r._tests_in(None, "tests/test_x.py", "head") == {}


def test_job_line_skips_blank_lines_in_a_block() -> None:
    assert r._job_line("jobs:\n  a:\n    name: x\n\n    if: y\n", "a", "if") == 5


# Edges found by mutation testing ----------------------------------------------------------------------------


def test_job_line_stops_at_the_next_job_and_matches_collapsed_spaces() -> None:
    text = "jobs:\n  a:\n    name: x\n  b:\n    if: y\n"
    assert r._job_line(text, "a", "if") == 2
    steps = "jobs:\n  a:\n    steps:\n      - if: p\n      - if: a  ==  b\n"
    assert r._job_line(steps, "a", "if", "a == b", step=True) == 5


def test_items_sort_dict_keys() -> None:
    assert r._items([{"b": 1, "a": 2}]) == ['{"a": 2, "b": 1}']


def test_thresholds_keep_their_names() -> None:
    assert set(r.thresholds("_PRIVATE_FLOOR = 3\nFLOOR_ = 4\n")) == {"_private_floor", "floor_"}


def test_parse_size_limits_are_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r, "MAX_PARSE_BYTES", 10)
    assert r._parse_toml("a = 1     ") == {"a": 1}
    assert r._parse_toml("a = 1      ") == "too large to check"
    assert r.load_workflow("w.yml", "jobs: {a: {}}"[:10]) is None
    assert r.load_workflow("w.yml", "jobs: {a: {}}"[:10] + "x") is None
    monkeypatch.setattr(r, "MAX_PARSE_BYTES", 13)
    assert r.load_workflow("w.yml", "jobs: {a: {}}") is not None
    assert r.load_workflow("w.yml", "jobs: {a: {}} ") is None
    assert r.load_workflow("w.yml", None) is None


def test_an_unreadable_toml_says_why() -> None:
    f = one(toml_findings("[tool.ruff\n"))
    assert "None" not in f.message
    assert "line 1" in f.message


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("[lint]\n", '[lint]\nextend-select = ["B"]\n', None),
        ('[lint]\nextend-select = ["B"]\n', "[lint]\n", "ruff extend-select no longer has B"),
        ("", 'exclude = ["x"]\n', "ruff exclude gains x"),
        ("", 'extend-exclude = ["y"]\n', "ruff extend-exclude gains y"),
        ("", '[lint.extend-per-file-ignores]\n"a: b/*.py" = ["S1"]\n', "ruff extend-per-file-ignores gains a: b/*.py: S1"),
    ],
)  # fmt: skip
def test_more_ruff_keys(old: str, new: str, message: str | None) -> None:
    found = toml_findings(new, "ruff.toml", old)
    assert [f.message.removeprefix("relaxes a check: ") for f in found] == ([message] if message else [])
    if message and "per-file" in message:
        assert found[0].line == 2


@pytest.mark.parametrize(
    ("old", "new", "messages"),
    [
        ("ignore_errors = false", "ignore_errors = true", ["mypy ignore_errors turned on"]),
        ("", 'follow_imports = "silent"', ["mypy follow_imports = silent"]),
        ('follow_imports = "skip"', 'follow_imports = "skip"\ncache_dir = "skip"', []),
        ('disable_error_code = ["a"]', 'disable_error_code = ["a", "b"]', ["mypy disable_error_code gains b"]),
        ('enable_error_code = ["a", "b"]', 'enable_error_code = ["a"]', ["mypy enable_error_code no longer has b"]),
        ('exclude = ["x"]', 'exclude = ["x", "y"]', ["mypy exclude gains y"]),
    ],
)  # fmt: skip
def test_more_mypy_keys(old: str, new: str, messages: list[str]) -> None:
    base = "[tool.mypy]\n" + old + "\n"
    found = toml_findings("[tool.mypy]\n" + new + "\n", old=base)
    assert [f.message.removeprefix("relaxes a check: ") for f in found] == messages
    for f in found:
        assert f.line > 1


def test_overrides_name_their_modules_and_add_to_the_top_level() -> None:
    new = PYPROJECT.replace("strict = true", "strict = false") + (
        '\n[[tool.mypy.overrides]]\nmodule = ["a", "b"]\nignore_errors = true\n'
        "\n[[tool.mypy.overrides]]\nignore_errors = true\n"
    )
    messages = sorted(f.message.removeprefix("relaxes a check: ") for f in toml_findings(new))
    assert messages == [
        "mypy override for ?: ignore_errors turned on",
        "mypy override for a, b: ignore_errors turned on",
        "mypy strict turned off",
    ]


def test_coverage_entries_already_there_are_not_reported() -> None:
    old = PYPROJECT.replace('source = ["pkg"]', 'source = ["pkg"]\nomit = ["a"]')
    assert toml_findings(old.replace('omit = ["a"]', 'omit = ["a"]\n'), old=old) == []
    f = one(toml_findings(old.replace('omit = ["a"]', 'omit = ["a", "b"]'), old=old))
    assert f.message == "relaxes a check: coverage run.omit gains b"


def test_toml_checks_every_changed_config() -> None:
    base = {"a.txt": "1", "pyproject.toml": "", "ruff.toml": "[broken\n"}
    head = {"a.txt": "2", "pyproject.toml": "[x\n", "ruff.toml": 'ignore = ["E1"]\n'}
    found = check(base, head, allow='ruff.toml :: ignore = ["E1"]')
    assert [(f.path, f.message[:20]) for f in found] == [("pyproject.toml", "could not read it to")]
    found = check(base, head)
    assert sorted(f.path for f in found) == ["pyproject.toml", "ruff.toml"]


def test_an_allowlisted_toml_line_does_not_hide_the_next() -> None:
    new = PYPROJECT.replace('ignore = ["E501"]', 'ignore = ["E501", "B008", "B009"]')
    found = toml_findings(new, allow='pyproject.toml :: select = ["E", "F", "S"]')
    assert len(found) == 2


def test_markers_check_every_changed_file() -> None:
    line = f"x = 1  {NOQA}"
    base = {"a.py": "", "b.py": "", "src/c.py": ""}
    head = {"b.py": f"\0{line}", "src/c.py": f"{line}\n{line}\n", "src/d.py": f"{line}\n"}
    found = check(base, head, allow=f"src/c.py :: {line}")
    assert [f.path for f in found] == ["src/d.py"]


def test_each_extra_copy_of_a_line_counts_once() -> None:
    line = f"x = 1  {NOQA}"
    found = check({"src/a.py": f"{line}\n{line}\n"}, {"src/a.py": f"{line}\n{line}\n{line}\n"})
    assert [f.line for f in found] == [3]


def test_thresholds_check_every_changed_file() -> None:
    base = {"src/a.py": "1", "Makefile": "A_FLOOR = 5\nB_FLOOR = 5\n"}
    head = {"src/a.py": "2", "Makefile": "\n", "scripts/x.sh": "C_FLOOR=1\n"}
    found = check(base, head)
    assert [(f.rule, f.path) for f in found] == [("threshold", "Makefile"), ("threshold", "Makefile")]


def test_parametrize_edges() -> None:
    source = """
@other
@pytest.mark.parametrize("a", [1, 2])
def test_x(a): ...

@pytest.mark.parametrize("b", argvalues=[1, 2, 3])
def test_y(b): ...

@pytest.mark.parametrize("c")
def test_z(c): ...
"""
    assert {k: v[0] for k, v in r.collect_tests(source).items()} == {"test_x": 2, "test_y": 3, "test_z": 1}


def test_test_classes_by_name_or_testcase_base() -> None:
    source = """
from unittest import TestCase
class Checks(TestCase):
    def test_a(self): ...
class Typed(Generic[T]):
    def test_b(self): ...
class Plain(object):
    def test_c(self): ...
"""
    assert set(r.collect_tests(source)) == {"Checks::test_a"}


def test_closure_keeps_going_past_a_repeated_need() -> None:
    wf = r.Workflow(CI, "", {"a": {"needs": ["b", "b", "c"]}, "b": {}, "c": {}})
    assert r.closure(wf, "a") == ["a", "b", "c"]


def test_parse_name_status_without_a_trailing_nul() -> None:
    assert r.parse_name_status("M\0a.py") == [Change("a.py", "a.py")]


def test_findings_name_the_workflow_path() -> None:
    new = CI_YML.replace("needs: [lint, test]", "needs: [test]").replace(
        "    runs-on: ubuntu-latest\n    steps:\n      - run: make lint",
        "    runs-on: ubuntu-latest\n    continue-on-error: true\n    if: x\n    steps:\n      - run: make lint\n"
        "        if: y",
    )
    found = required_findings(new, required=["Lint", "CI result"])
    assert {f.path for f in found} == {CI}
    assert len(found) == 4
    assert any("was '(none)'" in f.message for f in found)


def test_continue_on_error_already_there_passes() -> None:
    old = CI_YML.replace(
        "    runs-on: ubuntu-latest\n    steps:\n      - run: make lint",
        "    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n      - run: make lint",
    )
    assert required_findings(old, old=old) == []


def test_each_extra_copy_of_a_step_condition_counts_once() -> None:
    old = CI_YML.replace(
        "      - run: make test", "      - run: make test\n        if: x\n      - run: t2\n        if: x"
    )
    new = old + "      - run: t3\n        if: x\n"
    new = old.replace(
        "      - run: t2\n        if: x", "      - run: t2\n        if: x\n      - run: t3\n        if: x"
    )
    found = required_findings(new, old=old)
    assert len(found) == 1


def test_the_step_line_is_the_new_condition() -> None:
    old = CI_YML.replace("      - run: make test", "      - run: make test\n        if: a")
    new = old.replace("        if: a", "        if: a\n      - run: t2\n        if: b")
    f = one(required_findings(new, old=old))
    assert f.line == 20


def test_a_job_gaining_a_new_needed_job_is_compared_safely() -> None:
    new = CI_YML.replace("needs: [lint, test]", "needs: [lint, test, docs]") + (
        "  docs:\n    name: Docs\n    if: false\n    runs-on: ubuntu-latest\n    steps:\n      - run: x\n"
    )
    assert required_findings(new) == []


def test_several_required_checks_are_each_compared() -> None:
    new = CI_YML.replace("name: Lint", "name: Lint2").replace("    if: always()\n", "")
    found = required_findings(new, required=["Lint", "CI result"])
    assert sorted(f.message[:30] for f in found) == [
        "makes job 'result', which a re",
        "removes or renames the require",
    ]


def test_the_matching_producer_is_compared_not_the_first() -> None:
    other = ".github/workflows/a.yml"
    old = {CI: CI_YML, other: CI_YML.replace("    if: always()\n", "    if: x\n")}
    head = {CI: CI_YML, other: old[other]}
    found = [f for f in check(old, head, required=["CI result"]) if f.rule == "required check"]
    assert found == []


def test_main_checks_required_checks_through_git(repo: Path, tmp_path: Path) -> None:
    (repo / CI).write_text(CI_YML.replace("name: CI result", "name: CI summary"))
    head = commit(repo, "rename")
    required = tmp_path / "required.txt"
    required.write_text("CI result\n")
    code, out = run_main(
        "--repo", str(repo), "--base", "main", "--head", head, "--required-checks", str(required)
    )
    assert code == 1
    assert "removes or renames the required check 'CI result'" in out


def test_check_sealed_message() -> None:
    f = one(check({"eval/sealed/a": "1"}, {"eval/sealed/a": "2"}))
    assert f.message == "changes the sealed blind sets in eval/sealed/; no pull request changes them"

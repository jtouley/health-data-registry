"""Changelog fragments (issue #268): one small file per change in changelog.d/, folded into CHANGELOG.md at release.

Usage:
  python3 scripts/changelog.py check --base MERGE_BASE     # CI's Checks job, and make precheck
  python3 scripts/changelog.py assemble X.Y.Z [--date D]    # the release PR (make changelog VERSION=X.Y.Z)

`check` compares HEAD with its merge base with BASE (committed changes only) and fails when:
  - the change touches src/, catalog/, contracts/ or semantic/ and adds or changes no fragment;
  - the change edits CHANGELOG.md without deleting a fragment (only `assemble` writes CHANGELOG.md);
  - any fragment in changelog.d/ is malformed (so `assemble` can't fail at release time).
An empty BASE (main, the merge queue, nightly: no merge base to compare with) checks nothing and passes.

`assemble` moves every fragment's bullets, and whatever is under "## [Unreleased]", into a new
"## [X.Y.Z] - DATE" section, leaves an empty "## [Unreleased]" above it, updates the link references at the end,
and deletes the fragments. The release workflow reads that section for the release notes.

A fragment is changelog.d/<name>.md (name: letters, digits, '.', '_', '-'; the branch or PR, '/' as '-'). It holds
one or more "### Added", "### Changed", "### Deprecated", "### Removed", "### Fixed" or "### Security" headings,
each followed by Markdown bullets. changelog.d/README.md is the how-to and never a fragment.

Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = "changelog.d"
CHANGELOG = "CHANGELOG.md"
SECTIONS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
NEEDS_FRAGMENT = ("src/", "catalog/", "contracts/", "semantic/")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md")
VERSION = re.compile(r"\d+\.\d+\.\d+")
UNRELEASED = "## [Unreleased]"
HOWTO = "See changelog.d/README.md for how to write one."


class FragmentError(ValueError):
    """A fragment that can't be assembled."""


def is_fragment(path: str) -> bool:
    """Whether a repo-relative path is a fragment (not the README, not in a subfolder)."""
    parts = path.split("/")
    return (
        len(parts) == 2 and parts[0] == FRAGMENT_DIR and parts[1] != "README.md" and parts[1].endswith(".md")
    )


def parse_fragment(name: str, text: str) -> dict[str, str]:
    """A fragment's sections, in file order: heading -> its bullets (stripped). Raises FragmentError."""
    if not NAME.fullmatch(name):
        raise FragmentError(
            f"{name}: name it after the branch or PR, with letters, digits, '.', '_' or '-' only."
        )
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for n, line in enumerate(text.splitlines(), 1):
        heading = re.fullmatch(r"###\s+(.+?)\s*", line)
        if heading:
            title = heading.group(1)
            if title not in SECTIONS:
                raise FragmentError(f"{name}:{n}: '### {title}' is not one of {', '.join(SECTIONS)}.")
            if title in sections:
                raise FragmentError(
                    f"{name}:{n}: '### {title}' appears twice; put its bullets under one heading."
                )
            current = sections[title] = []
        elif line.startswith("#"):
            raise FragmentError(f"{name}:{n}: only '### <section>' headings belong in a fragment.")
        elif current is None:
            if line.strip():
                raise FragmentError(f"{name}:{n}: text before the first '### <section>' heading.")
        else:
            current.append(line.rstrip())
    if not sections:
        raise FragmentError(f"{name}: no '### <section>' heading. {HOWTO}")
    out: dict[str, str] = {}
    for title, lines in sections.items():
        body = "\n".join(lines).strip("\n")
        if not body.startswith("- "):
            raise FragmentError(f"{name}: '### {title}' needs at least one bullet ('- ...') right under it.")
        out[title] = body
    return out


def read_fragments(root: Path) -> list[tuple[str, dict[str, str]]]:
    """Every fragment in root/changelog.d, sorted by name, parsed. Raises FragmentError on the first bad one."""
    folder = root / FRAGMENT_DIR
    paths = sorted(p for p in folder.glob("*.md") if p.name != "README.md") if folder.is_dir() else []
    return [(p.name, parse_fragment(p.name, p.read_text(encoding="utf-8"))) for p in paths]


def _split_sections(block: str) -> tuple[str, dict[str, str]]:
    """The text of a release block: what precedes its first '### ' heading, and each heading's body."""
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current = preamble
    for line in block.splitlines():
        m = re.fullmatch(r"###\s+(.+?)\s*", line)
        if m:
            current = sections.setdefault(m.group(1), [])
        else:
            current.append(line)
    return "\n".join(preamble).strip("\n"), {k: "\n".join(v).strip("\n") for k, v in sections.items()}


def assemble_text(
    changelog: str, fragments: list[tuple[str, dict[str, str]]], version: str, date: str
) -> str:
    """CHANGELOG.md with the Unreleased content and the fragments moved into a new '## [version] - date' section."""
    if not VERSION.fullmatch(version):
        raise FragmentError(f"'{version}' is not a version like 1.2.3.")
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE):
        raise FragmentError(f"{CHANGELOG} already has a '## [{version}]' section.")
    lines = changelog.splitlines()
    try:
        start = lines.index(UNRELEASED)
    except ValueError:
        raise FragmentError(f"{CHANGELOG} has no '{UNRELEASED}' heading to release from.") from None
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("## ") or re.match(r"\[[^]]+\]: ", lines[i])
        ),
        len(lines),
    )
    preamble, sections = _split_sections("\n".join(lines[start + 1 : end]))
    existing = [k for k, v in sections.items() if v]
    for _, frag in fragments:
        for title, body in frag.items():
            sections[title] = f"{sections[title]}\n{body}" if sections.get(title) else body
    sections = {k: v for k, v in sections.items() if v}
    if not sections and not preamble:
        raise FragmentError(
            f"Nothing to release: no fragment in {FRAGMENT_DIR}/ and nothing under Unreleased."
        )
    # Sections already under Unreleased keep their order; new ones follow in Keep a Changelog's order.
    ordered = existing + [k for k in SECTIONS if k in sections and k not in existing]
    release = [UNRELEASED, "", f"## [{version}] - {date}", ""]
    if preamble:
        release += [preamble, ""]
    for title in ordered:
        release += [f"### {title}", "", sections[title], ""]
    tail = lines[end:]
    for i, line in enumerate(tail):
        m = re.fullmatch(r"\[Unreleased\]: (https://github\.com/[^/\s]+/[^/\s]+)/\S*", line)
        if m:
            repo = m.group(1)
            tail[i : i + 1] = [
                f"[Unreleased]: {repo}/compare/v{version}...HEAD",
                f"[{version}]: {repo}/releases/tag/v{version}",
            ]
            break
    return "\n".join(lines[:start] + release + tail).rstrip("\n") + "\n"


def assemble(root: Path, version: str, date: str) -> list[str]:
    """Write the release section into root/CHANGELOG.md and delete the fragments. Returns the fragments' names."""
    fragments = read_fragments(root)
    path = root / CHANGELOG
    path.write_text(
        assemble_text(path.read_text(encoding="utf-8"), fragments, version, date), encoding="utf-8"
    )
    for name, _ in fragments:
        (root / FRAGMENT_DIR / name).unlink()
    return [name for name, _ in fragments]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def changed(root: Path, base: str) -> list[tuple[str, str]]:
    """(status letter, path) for each file changed between the merge base of `base` and HEAD, committed only."""
    merge_base = _git(root, "merge-base", base, "HEAD").strip()
    out = _git(root, "diff", "--name-status", "--no-renames", merge_base, "HEAD", "--")
    rows = [line.split("\t", 1) for line in out.splitlines() if line]
    return [(status[0], path) for status, path in rows]


def check(changes: list[tuple[str, str]], root: Path) -> list[str]:
    """Plain-words problems with a change (empty when it passes)."""
    problems: list[str] = []
    touched = [p for _, p in changes if p.startswith(NEEDS_FRAGMENT)]
    added = [p for s, p in changes if s in "AM" and is_fragment(p)]
    deleted = [p for s, p in changes if s == "D" and is_fragment(p)]
    if touched and not added:
        problems.append(
            f"This change touches {touched[0]}"
            + (f" and {len(touched) - 1} more file(s)" if len(touched) > 1 else "")
            + f" under {', '.join(NEEDS_FRAGMENT)} but adds no changelog fragment. Add {FRAGMENT_DIR}/<branch>.md"
            f" with a '### Added', '### Changed' or '### Fixed' heading and a bullet saying what users will notice."
            f" {HOWTO}"
        )
    if any(p == CHANGELOG for _, p in changes) and not deleted:
        problems.append(
            f"This change edits {CHANGELOG} directly. Put the entry in a {FRAGMENT_DIR}/ fragment instead; only"
            f" `scripts/changelog.py assemble` writes {CHANGELOG}, at release. {HOWTO}"
        )
    try:
        read_fragments(root)
    except FragmentError as e:
        problems.append(f"A changelog fragment can't be assembled: {e} {HOWTO}")
    return problems


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description="Changelog fragments: check a change, or assemble a release.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="a change to src/, catalog/, contracts/ or semantic/ adds a fragment")
    c.add_argument(
        "--base", required=True, help="the branch or commit to compare with; empty skips the check"
    )
    a = sub.add_parser("assemble", help="fold the fragments into CHANGELOG.md under a release heading")
    a.add_argument("version", help="the release version, e.g. 0.3.0")
    a.add_argument(
        "--date", default=dt.datetime.now(dt.UTC).date().isoformat(), help="YYYY-MM-DD (default today)"
    )
    args = ap.parse_args(argv)
    if args.cmd == "assemble":
        try:
            names = assemble(root, args.version, args.date)
        except FragmentError as e:
            print(f"Not assembled: {e}", file=sys.stderr)
            return 1
        print(
            f"Wrote '## [{args.version}] - {args.date}' in {CHANGELOG} from {len(names)} fragment(s); deleted them."
        )
        return 0
    if not args.base:
        print("No merge base (main, the merge queue or nightly): the changelog fragment check is skipped.")
        return 0
    problems = check(changed(root, args.base), root)
    for p in problems:
        print(f"::error::{p}" if "GITHUB_ACTIONS" in os.environ else p, file=sys.stderr)
    if not problems:
        print("Changelog fragments: OK.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

"""Check the installed dbt packages are exactly the reviewed ones (make transform, and the refresh's transform job).

A hub package version is a git tag upstream, and a tag can be moved; `dbt deps` checks no digest of what it
downloads, and package-lock.yml's sha1_hash hashes the packages.yml spec, not the code. These macros compile into
every dbt-expectations test the release gate judges, so a changed macro could turn a check into a constant pass.
This checks, for each package in transform/package-lock.yml:

  - packages.yml pins the same package at the same version (so `dbt deps` has nothing to re-resolve);
  - package-digests.yml holds a reviewed digest for that version;
  - the sha256 of every installed file in dbt_packages/<name> is that digest.

Exit 0 when all hold; exit 1, naming what differs, otherwise. With --installed it only says whether the locked
install is already in place (make transform then skips `dbt deps`, so it needs the network only once).
With --print it prints each installed package's digest, for a reviewed version bump.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

import yaml

TRANSFORM = Path(__file__).resolve().parent.parent / "transform"
INSTALLED = TRANSFORM / "dbt_packages"


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def tree_digest(root: Path) -> str:
    """The sha256 of a directory: every file's relative path and sha256, in path order."""
    h = hashlib.sha256()
    for p in sorted(q for q in root.rglob("*") if q.is_file()):
        h.update(p.relative_to(root).as_posix().encode() + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode() + b"\n")
    return h.hexdigest()


def problems() -> list[str]:
    lock = _yaml(TRANSFORM / "package-lock.yml").get("packages") or []
    pinned = {
        p.get("package"): str(p.get("version"))
        for p in _yaml(TRANSFORM / "packages.yml").get("packages") or []
    }
    digests = _yaml(TRANSFORM / "package-digests.yml").get("packages") or {}
    out = []
    if set(pinned) != {p.get("package") for p in lock}:
        out.append("packages.yml and package-lock.yml list different packages")
    for p in lock:
        name, package, version = p.get("name"), p.get("package"), str(p.get("version"))
        if pinned.get(package) != version:
            out.append(f"{package}: packages.yml pins {pinned.get(package)}, the lock {version}")
        want = digests.get(name) or {}
        if str(want.get("version")) != version or not want.get("sha256"):
            out.append(f"{name}: package-digests.yml has no reviewed digest for {version}")
            continue
        # The package's own dbt_project.yml version need not match the hub's tag (dbt_expectations 0.10.10 says
        # 0.6.0), so the digest, not that field, says which release is installed.
        if not (INSTALLED / name / "dbt_project.yml").is_file():
            out.append(f"{name}: not installed in dbt_packages/")
            continue
        got = tree_digest(INSTALLED / name)
        if got != want["sha256"]:
            out.append(
                f"{name} {version}: installed files have sha256 {got}, not the reviewed {want['sha256']}"
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--installed", action="store_true", help="only say whether the locked install is in place"
    )
    ap.add_argument("--print", action="store_true", help="print each installed package's digest")
    a = ap.parse_args(argv)
    if a.print:
        for d in sorted(q for q in INSTALLED.iterdir() if q.is_dir()):
            print(f"{d.name}: {tree_digest(d)}")
        return 0
    found = problems()
    if a.installed:
        print(
            "dbt packages: the locked install is in place"
            if not found
            else "dbt packages: installing (dbt deps)"
        )
        return 1 if found else 0
    if found:
        print("The dbt packages are not the reviewed ones:\n  " + "\n  ".join(found), file=sys.stderr)
        return 1
    print("dbt packages: every package is the locked version with its reviewed sha256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

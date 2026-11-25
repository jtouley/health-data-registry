"""Keep fuzz/requirements.txt in lockstep with uv.lock.

The ClusterFuzzLite image runs Python 3.11 and uv.lock is resolved for 3.12+, so the fuzz build can't install from
the lock directly (it has no 3.11 wheels). Instead:

    uv run python scripts/fuzz_requirements.py write   # make fuzz-requirements; needs PyPI
    uv run python scripts/fuzz_requirements.py check   # offline; tests/test_fuzz_harnesses.py runs it in CI

`write` pins every package in uv.lock to its locked version (as constraints) and compiles fuzz/requirements.in
for 3.11 with hashes, so each version comes from the lock and each hash from PyPI. `check` fails when any
package in fuzz/requirements.txt has a version other than uv.lock's, or is not in uv.lock at all. Dependabot
bumps uv.lock only; after a bump, run `make fuzz-requirements` and commit the result.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "uv.lock"
REQ_IN = ROOT / "fuzz" / "requirements.in"
REQ_TXT = ROOT / "fuzz" / "requirements.txt"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", re.MULTILINE)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(lock: Path = LOCK) -> dict[str, str]:
    """Package name (normalised) -> the version uv.lock pins."""
    data = tomllib.loads(lock.read_text())
    return {_norm(p["name"]): str(p["version"]) for p in data.get("package", []) if "version" in p}


def pinned_versions(req: Path = REQ_TXT) -> dict[str, str]:
    """Package name (normalised) -> the version a requirements file pins."""
    return {_norm(name): version for name, version in _PIN.findall(req.read_text())}


def mismatches(lock: Path = LOCK, req: Path = REQ_TXT) -> list[str]:
    """One line per package in the requirements file whose version differs from uv.lock's, or is not locked."""
    locked = locked_versions(lock)
    out = []
    for name, version in sorted(pinned_versions(req).items()):
        if name not in locked:
            out.append(f"{name}=={version} is in {req.name} but not in uv.lock")
        elif locked[name] != version:
            out.append(f"{name}=={version} in {req.name}, but uv.lock has {locked[name]}")
    return out


def write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        constraints = Path(tmp) / "constraints.txt"
        constraints.write_text("".join(f"{n}=={v}\n" for n, v in sorted(locked_versions().items())))
        subprocess.run(  # noqa: S603 - fixed arguments
            [  # noqa: S607 - uv from PATH, as in the Makefile
                "uv", "pip", "compile", "--quiet", "--python-version", "3.11",
                "--python-platform", "x86_64-manylinux_2_28", "--generate-hashes", "--no-header", "--no-annotate",
                "--constraints", str(constraints), str(REQ_IN.relative_to(ROOT)),
                "-o", str(REQ_TXT.relative_to(ROOT)),
            ],
            check=True,
            cwd=ROOT,
        )  # fmt: skip
    header = (
        "# Generated from uv.lock by scripts/fuzz_requirements.py (make fuzz-requirements); don't edit by hand.\n"
        "# Versions are uv.lock's; hashes are PyPI's for the ClusterFuzzLite image (Python 3.11, manylinux).\n"
    )
    REQ_TXT.write_text(header + REQ_TXT.read_text())


def main(argv: list[str]) -> int:
    if argv[1:] == ["write"]:
        write()
    elif argv[1:] != ["check"]:
        print("usage: fuzz_requirements.py write|check", file=sys.stderr)
        return 2
    problems = mismatches()
    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        print("Run `make fuzz-requirements` and commit fuzz/requirements.txt.", file=sys.stderr)
        return 1
    print(f"{REQ_TXT.relative_to(ROOT)} matches uv.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

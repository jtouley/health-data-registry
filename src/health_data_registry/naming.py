"""Hugging Face repository names: the one place every repo id of this project is built.

Every Hugging Face repository of the project starts ``<owner>/health-data-registry`` (docs/dev/naming.md):

    <owner>/health-data-registry                          public dataset (the open data release)
    <owner>/health-data-registry-mcp                      the Space that hosts the server
    <owner>/health-data-registry-private-<source-id>      private tier, one dataset per catalog source
    <owner>/health-data-registry-ops-<job>                operational state of one workflow job

``<source-id>`` is the catalog id with ``_`` changed to ``-``. Names are lowercase, hyphens only, and never
abbreviated. Code, workflows and docs build names here (workflows through ``python -m
health_data_registry.naming``) and never spell them out.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from typing import Final

PROJECT: Final = "health-data-registry"
DEFAULT_OWNER: Final = "jtouley"
OWNER_ENV: Final = "HDR_PRIVATE_HF_OWNER"
SPACE_SUFFIX: Final = "mcp"
PRIVATE_TIER: Final = "private"
OPS_TIER: Final = "ops"

_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_JOB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


class BadName(ValueError):
    """A name part that the convention does not allow."""


def owner(value: str | None = None) -> str | None:
    """The Hugging Face owner: ``value``, else ``HDR_PRIVATE_HF_OWNER``; None when neither is set."""
    found = (value if value is not None else os.environ.get(OWNER_ENV, "")).strip()
    if not found:
        return None
    if not _OWNER.fullmatch(found):
        raise BadName(f"{found!r} is not a Hugging Face user or organisation name")
    return found


def _repo(owner_name: str, *parts: str) -> str:
    checked = owner(owner_name)
    if checked is None:
        raise BadName("no Hugging Face owner given")
    return f"{checked}/{'-'.join((PROJECT, *parts))}"


def public_dataset(owner_name: str = DEFAULT_OWNER) -> str:
    return _repo(owner_name)


def space(owner_name: str = DEFAULT_OWNER) -> str:
    return _repo(owner_name, SPACE_SUFFIX)


def source_slug(source_id: str) -> str:
    """The catalog id as it appears in a repo name: ``croi_abstracts_index`` -> ``croi-abstracts-index``."""
    if not _SOURCE_ID.fullmatch(source_id) or "__" in source_id or source_id.endswith("_"):
        raise BadName(f"{source_id!r} is not a catalog source id (snake_case letters and digits)")
    return source_id.replace("_", "-")


def private_dataset(source_id: str, owner_name: str = DEFAULT_OWNER) -> str:
    return _repo(owner_name, PRIVATE_TIER, source_slug(source_id))


def ops_dataset(job: str, owner_name: str = DEFAULT_OWNER) -> str:
    if not _JOB.fullmatch(job):
        raise BadName(f"{job!r} is not a job name (lowercase words joined by hyphens)")
    return _repo(owner_name, OPS_TIER, job)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m health_data_registry.naming", description="Print one Hugging Face repo id."
    )
    parser.add_argument("--owner", help=f"default: ${OWNER_ENV}, else {DEFAULT_OWNER}")
    sub = parser.add_subparsers(dest="kind", required=True)
    sub.add_parser("public")
    sub.add_parser("space")
    sub.add_parser("private").add_argument("source_id")
    sub.add_parser("ops").add_argument("job")
    args = parser.parse_args(argv)
    try:
        who = owner(args.owner) or DEFAULT_OWNER
        if args.kind == "public":
            name = public_dataset(who)
        elif args.kind == "space":
            name = space(who)
        elif args.kind == "private":
            name = private_dataset(args.source_id, who)
        else:
            name = ops_dataset(args.job, who)
    except BadName as e:
        print(e, file=sys.stderr)
        return 2
    print(name)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Hugging Face repository names: the one place every repo id of this project is built.

Every Hugging Face repository of the project starts ``<owner>/health-data-registry`` (docs/dev/naming.md):

    <owner>/health-data-registry                          public dataset (the open data release)
    <owner>/health-data-registry-mcp                      the Space that hosts the server
    <owner>/health-data-registry-private-<source-id>      private tier, one dataset per catalog source
    <owner>/health-data-registry-ops-<job>                operational state of one workflow job

``<source-id>`` is the catalog id with ``_`` changed to ``-``. Names are lowercase, hyphens only, and never
abbreviated. Code, workflows and docs build names here (workflows through ``python -m
health_data_registry.naming``) and never spell them out.

Owners: the public dataset and the Space take ``HDR_HF_OWNER``; private and ops datasets take
``HDR_PRIVATE_HF_OWNER``. Both default to ``jtouley``. Before any upload to a private or ops dataset,
``assert_private`` (or ``python -m health_data_registry.naming assert-private <repo>``) checks that the repo is
one of this owner's private or ops names, creates it private if it is missing, and refuses unless Hugging Face
reports it private: ``hf upload --private`` is ignored for a repo that already exists.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from typing import Any, Final

PROJECT: Final = "health-data-registry"
DEFAULT_OWNER: Final = "jtouley"
OWNER_ENV: Final = "HDR_PRIVATE_HF_OWNER"
PUBLIC_OWNER_ENV: Final = "HDR_HF_OWNER"
SPACE_SUFFIX: Final = "mcp"
PRIVATE_TIER: Final = "private"
OPS_TIER: Final = "ops"

_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_JOB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class BadName(ValueError):
    """A name part that the convention does not allow."""


class NotPrivate(RuntimeError):
    """A repo that private data would be uploaded to is not a private repo of the private owner."""


def owner(value: str | None = None, *, env: str = OWNER_ENV) -> str | None:
    """The Hugging Face owner: ``value``, else the ``env`` variable (``HDR_PRIVATE_HF_OWNER``); None if unset."""
    found = (value if value is not None else os.environ.get(env, "")).strip()
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


def private_owner() -> str:
    """The owner of every private and ops dataset: ``HDR_PRIVATE_HF_OWNER``, else ``jtouley``."""
    return owner() or DEFAULT_OWNER


def assert_private(api: Any, repo_id: str, *, repo_type: str = "dataset") -> str:
    """Make sure ``repo_id`` is a private repo before private data goes to it; returns it, or raises.

    The repo must be ``<HDR_PRIVATE_HF_OWNER>/health-data-registry-private-...`` or ``...-ops-...``, so neither a
    variable nor a script can point an upload at another account or at the public dataset. It is created private
    when missing; if Hugging Face then reports it public (it was made public by hand), nothing is uploaded.
    """
    who = private_owner()
    prefixes = tuple(f"{who}/{PROJECT}-{tier}-" for tier in (PRIVATE_TIER, OPS_TIER))
    if not repo_id.startswith(prefixes) or not _NAME.fullmatch(repo_id.partition("/")[2]):
        raise NotPrivate(
            f"{repo_id!r} is not a private or ops dataset of {who} (docs/dev/naming.md); nothing was uploaded"
        )
    api.create_repo(repo_id, repo_type=repo_type, private=True, exist_ok=True)
    if api.repo_info(repo_id, repo_type=repo_type).private is not True:
        raise NotPrivate(f"{repo_id} is public on Hugging Face; private data is never uploaded to it")
    return repo_id


def main(argv: Sequence[str] | None = None, *, api: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m health_data_registry.naming", description="Print one Hugging Face repo id."
    )
    parser.add_argument(
        "--owner",
        help=f"default: ${PUBLIC_OWNER_ENV} for public and space, ${OWNER_ENV} otherwise; else {DEFAULT_OWNER}",
    )
    sub = parser.add_subparsers(dest="kind", required=True)
    sub.add_parser("public")
    sub.add_parser("space")
    sub.add_parser("private").add_argument("source_id")
    sub.add_parser("ops").add_argument("job")
    sub.add_parser(
        "assert-private",
        help="create the repo private if missing; exit 1 unless it is a private repo of the owner",
    ).add_argument("repo_id")
    args = parser.parse_args(argv)
    if args.kind == "assert-private":
        return _assert_private_cli(args.repo_id, api)
    try:
        env = PUBLIC_OWNER_ENV if args.kind in ("public", "space") else OWNER_ENV
        who = owner(args.owner, env=env) or DEFAULT_OWNER
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


def _assert_private_cli(repo_id: str, api: Any = None) -> int:
    if api is None:  # pragma: no cover - the real Hub client; tests pass a fake
        from huggingface_hub import HfApi

        api = HfApi(token=os.environ.get("HF_TOKEN") or None)
    try:
        assert_private(api, repo_id)
    except (BadName, NotPrivate) as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    print(f"{repo_id} is private")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

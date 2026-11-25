"""The build manifest (ADR 0017 section 2): one versioned, immutable file naming everything a transform build read.

A build is one `dbt build` of the transform project. It has two steps, so dbt never resolves `LATEST` itself:

    pin       `pin-build` (before dbt runs): resolves each stored source's LATEST pointer once and writes the
              build's pinned part to transform/target/build-pin.json. The dbt_raw plugin reads only the loads the
              pin names (HDR_BUILD_PIN) and refuses a records file whose sha256 no longer matches.
    record    `gate` (after dbt runs): re-derives the pin from its own checkout and lake and holds the release
              unless it matches, judges dbt's run record, and writes builds/<build_id>/build.json with the pinned
              part and the quality gate's verdict. On a pass it points builds/LATEST at the build.

The pinned part, which alone decides the build id ("build-" + the sha256 of its canonical JSON, keys sorted):

    manifest_version  the layout of this file (MANIFEST_VERSION)
    inputs            per stored source: the snapshot id LATEST named, its raw sha256s (in the snapshot manifest's
                      order), whether it is a partial sample, and the records file dbt read with its sha256 (null
                      when dbt read nothing: no load, a partial sample, or no kept records)
    control_plane     each control-plane export read, with its sha256 (none yet: stage c adds the first)
    contracts         id, version, path and sha256 of each source contract and each model contract
    semantic          version, path and sha256 of each stored source's semantic model
    transform         path -> sha256 of every file of the dbt project (generated staging included)
    code              the git commit, the sha256 of uv.lock, and for uncommitted changes the sha256 of the diff of
                      tracked files plus the untracked, not-ignored files (null for a clean checkout)
    engines           Python, dbt-core, dbt-duckdb and DuckDB versions, as installed where dbt ran
    vars              the dbt vars in transform/dbt_project.yml (the gate holds a run given --vars on the command
                      line, since they would not be pinned)

The same inputs give the same id and the same bytes, whenever and wherever the build runs: there is no clock, host
or thread count in the file. Those, with dbt's run id, go to the gate's run log (ops/builds.jsonl). The verdict
lists each node by name, never by position, and carries no free-text error (dbt's messages can name paths).

A build directory is written once: build.json is made read-only and linked into place, so it is never rewritten.
A second run of the same build that reaches a different verdict is refused, not recorded: the same inputs must give
the same result.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import subprocess  # noqa: S404 - git only, with fixed arguments
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
PIN_FILE = "build-pin.json"
MANIFEST_FILE = "build.json"
BUILDS = "builds"
LATEST = "LATEST"
DBT_PROJECT = Path("transform/dbt_project.yml")
TRANSFORM = Path("transform")
# dbt's own output and per-user files in transform/: never an input (transform/.gitignore).
TRANSFORM_SKIP = frozenset({"target", "dbt_packages", "logs", ".user.yml"})
PINNED_KEYS = (
    "manifest_version",
    "inputs",
    "control_plane",
    "contracts",
    "semantic",
    "transform",
    "code",
    "engines",
    "vars",
)
BUILD_ID = re.compile(r"build-[0-9a-f]{64}")
SNAPSHOT_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{12}")
ENGINES = ("dbt-core", "dbt-duckdb", "duckdb")


class BuildManifestError(ValueError):
    """A pin or build manifest can't be made or read; the message says why in plain words."""


class BuildManifestConflict(BuildManifestError):
    """builds/<build_id>/build.json already holds different bytes: the same inputs gave a different result."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def canonical(doc: Any) -> bytes:
    """The bytes the build id hashes: JSON with sorted keys and no spaces."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def build_id_of(pinned: Mapping[str, Any]) -> str:
    """The same pinned inputs always give the same id."""
    return "build-" + hashlib.sha256(canonical(pinned)).hexdigest()


def render(doc: Mapping[str, Any]) -> bytes:
    """The file's bytes: sorted keys, one-space indent, a final newline. Same document, same bytes."""
    return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


# ------------------------------------------------------------------------------------------------ the pinned part


def records_path(dataset_id: str, snapshot_id: str) -> str:
    """Where a load's records live, relative to the lake (store.keep_raw_records)."""
    return f"raw_records/{dataset_id}/{snapshot_id}.ndjson"


def resolve_inputs(registry: Any, lake: Path) -> dict[str, Any]:
    """Each stored source's LATEST load, resolved once: what dbt will read, and the hash of every byte of it."""
    from health_data_registry.store import latest_snapshot
    from health_data_registry.transform_gen import stored

    out: dict[str, Any] = {}
    for ds in stored(registry):
        try:
            found = latest_snapshot(lake, ds.id)
        except (OSError, ValueError) as e:
            raise BuildManifestError(
                f"{ds.id}: the lake's LATEST pointer or its snapshot manifest can't be read ({e}); run "
                f"`health-data-registry refresh {ds.id}` and pin the build again"
            ) from e
        if found is None:
            out[ds.id] = {"snapshot_id": None, "raw_sha256": [], "partial": False, "records": None}
            continue
        m = found[1]
        rel = records_path(ds.id, m.snapshot_id)
        records = None
        if not m.partial and (lake / rel).is_file():
            records = {"path": rel, "sha256": sha256_file(lake / rel)}
        out[ds.id] = {
            "snapshot_id": m.snapshot_id,
            "raw_sha256": list(m.raw_sha256),
            "partial": m.partial,
            "records": records,
        }
    return out


def contract_files(registry: Any) -> dict[str, Any]:
    """Every contract the build reads: each stored source's and each model's, with version and file hash."""
    from health_data_registry.transform_gen import model_contracts, stored

    root = registry.root
    sources = {
        ds.id: {
            "id": ds.contract.id,
            "version": ds.contract.version,
            "path": Path(ds.entry.contract).as_posix(),
            "sha256": sha256_file(root / ds.entry.contract),
        }
        for ds in stored(registry)
    }
    models = {
        c.id: {"id": c.id, "version": c.version, "path": p.as_posix(), "sha256": sha256_file(root / p)}
        for p, c in model_contracts(root)
    }
    return {"sources": sources, "models": models}


def semantic_files(registry: Any) -> dict[str, Any]:
    from health_data_registry.transform_gen import stored

    return {
        ds.id: {
            "version": ds.semantic.version,
            "path": Path(ds.entry.semantic_model).as_posix(),
            "sha256": sha256_file(registry.root / ds.entry.semantic_model),
        }
        for ds in stored(registry)
    }


def transform_files(root: Path) -> dict[str, str]:
    """path -> sha256 of every file of the dbt project, leaving out dbt's own output."""
    base = root / TRANSFORM
    out: dict[str, str] = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if p.is_file() and rel.parts[0] not in TRANSFORM_SKIP:
            out[(TRANSFORM / rel).as_posix()] = sha256_file(p)
    return out


def dbt_vars(root: Path) -> dict[str, Any]:
    import yaml

    doc = yaml.safe_load((root / DBT_PROJECT).read_text(encoding="utf-8")) or {}
    return dict(doc.get("vars") or {})


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(  # noqa: S603 - fixed git arguments, no shell
        ["git", "-C", str(root), *args],  # noqa: S607 - git from PATH, as every developer and CI runs it
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout


def code_state(root: Path, *, commit: str | None = None) -> dict[str, Any]:
    """The code a build ran: the git commit, uv.lock's hash, and a hash of any uncommitted change.

    `commit`, when given (--commit, or $GITHUB_SHA in CI), must be the checkout's HEAD."""
    try:
        head = _git(root, "rev-parse", "HEAD").decode().strip()
        diff = _git(root, "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv")
        untracked = sorted(
            p for p in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise BuildManifestError(
            f"the code's commit can't be read: {root} is not a git checkout git can read ({type(e).__name__})"
        ) from e
    if commit and commit != head:
        raise BuildManifestError(
            f"the checkout is at commit {head}, not {commit}, the commit this build names"
        )
    uncommitted = None
    if diff or untracked:
        h = hashlib.sha256(diff)
        for rel in untracked:
            path = root / rel.decode()
            h.update(b"\0" + rel + b"\0" + (sha256_file(path).encode() if path.is_file() else b"-"))
        uncommitted = h.hexdigest()
    lock = root / "uv.lock"
    if not lock.is_file():
        raise BuildManifestError(f"{lock} is missing, so the dependencies the build ran with can't be pinned")
    return {"commit": head, "uv_lock_sha256": sha256_file(lock), "uncommitted_sha256": uncommitted}


def engines() -> dict[str, str | None]:
    """The versions installed here; a package that isn't installed is null."""
    out: dict[str, str | None] = {"python": platform.python_version()}
    for name in ENGINES:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def pin(
    registry: Any,
    lake: Path,
    *,
    commit: str | None = None,
    code: Mapping[str, Any] | None = None,
    engine_versions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The build's pinned part. `code` and `engine_versions` replace what this machine reports (the gate takes the
    engines from the pin made where dbt ran)."""
    import yaml

    root = registry.root
    try:
        return {
            "manifest_version": MANIFEST_VERSION,
            "inputs": resolve_inputs(registry, lake),
            "control_plane": {},
            "contracts": contract_files(registry),
            "semantic": semantic_files(registry),
            "transform": transform_files(root),
            "code": dict(code) if code is not None else code_state(root, commit=commit),
            "engines": dict(engine_versions) if engine_versions is not None else engines(),
            "vars": dbt_vars(root),
        }
    except BuildManifestError:
        raise
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise BuildManifestError(f"the build's inputs could not be read ({e})") from e


def _check_pinned(doc: Any, where: str) -> dict[str, Any]:
    if not isinstance(doc, dict) or set(doc) != set(PINNED_KEYS):
        raise BuildManifestError(f"{where} is not a build pin this version can read")
    if doc["manifest_version"] != MANIFEST_VERSION:
        raise BuildManifestError(
            f"{where} has build manifest version {doc['manifest_version']!r}; this version reads "
            f"{MANIFEST_VERSION}"
        )
    if not isinstance(doc["inputs"], dict):
        raise BuildManifestError(f"{where} has no inputs")
    for ds_id, entry in doc["inputs"].items():
        sid = entry.get("snapshot_id") if isinstance(entry, dict) else "?"
        if sid is not None and not (isinstance(sid, str) and SNAPSHOT_ID.fullmatch(sid)):
            raise BuildManifestError(f"{where}: {ds_id} names {sid!r}, which is not a snapshot id")
    return doc


def write_pin(path: Path, pinned: Mapping[str, Any]) -> str:
    """Write the pin (before dbt runs); returns the build id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(render(pinned))
    os.replace(tmp, path)
    return build_id_of(pinned)


def read_pin(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BuildManifestError(
            f"the build's pin {path} can't be read ({type(e).__name__}), so what the build read is unknown"
        ) from e
    return _check_pinned(doc, str(path))


def differences(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Where a pin made elsewhere differs from the one made here, engines aside (they are where dbt ran)."""
    out = []
    for key in PINNED_KEYS:
        if key == "engines":
            continue
        a, b = recorded.get(key), current.get(key)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            out += [f"{key}.{k}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        else:
            out.append(key)
    return out


# ------------------------------------------------------------------------------------------------ the verdict

_PRECEDENCE = ("fail", "rule error", "not evaluated", "warn", "pass")


def verdict_of(build: Any) -> dict[str, Any]:
    """The quality gate's verdict on a release_gate.Build: every test by name, each model that failed or was
    skipped, and why the build is held. Counts and names only, no free text from dbt, so it repeats exactly."""
    results: dict[str, dict[str, Any]] = {}

    def put(name: str, outcome: str, failures: Any = None) -> None:
        old = results.get(name)
        if old is None or _PRECEDENCE.index(outcome) < _PRECEDENCE.index(old["outcome"]):
            count = failures if isinstance(failures, int) and not isinstance(failures, bool) else None
            results[name] = {"outcome": outcome, "failures": count}

    for name in build.passed_tests:
        put(name, "pass", 0)
    for w in build.warnings:
        put(str(w["test"]), "warn", w.get("failures"))
    for f in build.data_failures:
        put(str(f["test"]), "fail", f.get("failures"))
    for r in build.rule_errors:
        put(str(r["test"]), "rule error")
    for n in build.not_evaluated:
        put(str(n["test"]), "not evaluated")
    return {
        "status": "pass" if build.servable else "hold",
        "reason": build.reason() or None,
        "models_built": build.models_built,
        "model_errors": sorted({str(m["model"]) for m in build.model_errors}),
        "skipped": sorted({str(s["node"]) for s in build.skipped}),
        "incomplete": sorted(set(build.incomplete)),
        "input_problems": sorted(set(build.input_problems)),
        "tests": dict(sorted(results.items())),
    }


def manifest_of(pinned: Mapping[str, Any], build: Any) -> dict[str, Any]:
    return {"build_id": build_id_of(pinned), "pinned": dict(pinned), "verdict": verdict_of(build)}


# ------------------------------------------------------------------------------------------------ on disk


def build_dir(lake: Path, build_id: str) -> Path:
    if not BUILD_ID.fullmatch(build_id):
        raise BuildManifestError(f"{build_id!r} is not a build id")
    return lake / BUILDS / build_id


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError):  # not every file system lets a directory be opened for fsync
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_manifest(lake: Path, manifest: Mapping[str, Any]) -> Path:
    """Write builds/<build_id>/build.json once. The same bytes again are a no-op (the file is untouched); different
    bytes are refused with BuildManifestConflict and nothing is written. The file is read-only before its name
    exists (os.link fails if the name is taken, unlike os.replace), so it is never seen half-written."""
    build_id = str(manifest["build_id"])
    if build_id != build_id_of(manifest["pinned"]):
        raise BuildManifestError(f"{build_id} is not the hash of its pinned inputs")
    data = render(manifest)
    folder = build_dir(lake, build_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / MANIFEST_FILE

    def settled() -> Path:
        if target.read_bytes() == data:
            return target
        raise BuildManifestConflict(
            f"build {build_id} already has a build manifest at {target} that differs from this run's, so the same "
            "inputs gave a different result. Nothing was written and the release is held; compare the two runs in "
            "the gate's run log (ops/builds.jsonl)."
        )

    if target.exists():
        return settled()
    tmp = folder / f".{MANIFEST_FILE}.{os.getpid()}.tmp"
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(0o444)
        try:
            os.link(tmp, target)
        except FileExistsError:  # another run wrote it between the check and the link
            return settled()
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(folder)
    return target


def point_latest(lake: Path, build_id: str) -> None:
    """builds/LATEST names the last build that passed; swapped in one step, never read inside a build."""
    folder = lake / BUILDS
    build_dir(lake, build_id)  # validates the id
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{LATEST}.{os.getpid()}.tmp"
    tmp.write_text(build_id + "\n", encoding="utf-8")
    os.replace(tmp, folder / LATEST)


def read_manifest(path: Path) -> dict[str, Any]:
    """A build manifest, checked: its layout, and that its build id is the hash of its pinned part."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BuildManifestError(f"the build manifest {path} can't be read ({type(e).__name__})") from e
    if not isinstance(doc, dict) or set(doc) != {"build_id", "pinned", "verdict"}:
        raise BuildManifestError(f"{path} is not a build manifest this version can read")
    _check_pinned(doc["pinned"], str(path))
    if doc["build_id"] != build_id_of(doc["pinned"]):
        raise BuildManifestError(
            f"{path}: its build id is not the hash of what it says the build read, so it was changed after it was "
            "written"
        )
    if not isinstance(doc["verdict"], dict) or doc["verdict"].get("status") not in ("pass", "hold"):
        raise BuildManifestError(f"{path} has no quality gate verdict")
    return doc


def latest_build(lake: Path) -> tuple[Path, dict[str, Any]] | None:
    """The build builds/LATEST names, read and checked; None when no build has passed in this lake."""
    pointer = lake / BUILDS / LATEST
    if not pointer.is_file():
        return None
    build_id = pointer.read_text(encoding="utf-8").strip()
    path = build_dir(lake, build_id) / MANIFEST_FILE
    return path, read_manifest(path)


def snapshot_problems(manifest: Mapping[str, Any], snapshots: Mapping[str, Any]) -> list[str]:
    """Each source whose staged snapshot is not the one the build read (`snapshots`: id -> store.Manifest)."""
    inputs = manifest["pinned"]["inputs"]
    build_id = manifest["build_id"]
    out = []
    for ds_id, snap in sorted(snapshots.items()):
        read = inputs.get(ds_id)
        if read is None:
            out.append(f"{ds_id}: build {build_id} did not read this source")
        elif read.get("snapshot_id") != snap.snapshot_id or list(read.get("raw_sha256") or []) != list(
            snap.raw_sha256
        ):
            out.append(
                f"{ds_id}: build {build_id} read snapshot {read.get('snapshot_id')}, but the lake's LATEST is "
                f"{snap.snapshot_id}"
            )
        elif read.get("records") is None:
            out.append(f"{ds_id}: build {build_id} read no records for snapshot {snap.snapshot_id}")
    return out


def summary(pinned: Mapping[str, Any]) -> Sequence[str]:
    """One line per input, for the CLI."""
    lines = []
    for ds_id, entry in sorted(pinned["inputs"].items()):
        lines.append(
            f"  {ds_id}: {entry['snapshot_id'] or 'no load'}" + ("" if entry["records"] else " (empty)")
        )
    return lines

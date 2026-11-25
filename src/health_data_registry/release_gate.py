"""The release gate: a hub release is served only when its transform build passed every error-severity test.

After `dbt build`, the gate reads dbt's own record of the run (target/run_results.json, and target/manifest.json
for each node's resource type, severity and dimension) and sorts every result into one of these outcomes:

    model error     a model, seed or snapshot failed to build, or one the project declares has no result
    skipped         a node was skipped because something it depends on failed (reported apart, so a failed
                    check upstream is not counted as a model that failed)
    rule error      a test could not run (its SQL no longer fits the table, or it measured nothing)
    data failure    an error-severity test, or a unit test, ran and failed
    warning         a warn-severity test failed: served, and the warning travels with the answers
    incomplete      the run record has no results, or no test or unit test ran: nothing was judged

The gate fails closed: any result type it does not know, with any status but success, holds the build too.

Two steps, so "served" is only ever recorded for a release that was uploaded:

    check     `gate --target T --ops O`: records the build in ops/builds.jsonl and its verdict, pass or hold, in
              ops/serving_log.jsonl. A hold exits 1, so the refresh uploads nothing and the release served
              before stays served. ops/SERVING is not touched.
    confirm   `gate --confirm <tag>`: after export-public has uploaded and tagged the release, records
              `serve` with the exact tag export-public created (release-<run date>, or release-<run date>.N for
              a correction) and swaps ops/SERVING to it. Only a build that passed the check can be confirmed.

The three files live under the lake ($HDR_LAKE/ops by default) and never in git (hard rule 5). The weekly refresh
keeps them between runs in the private ops dataset <owner>/health-data-registry-ops-release-gate
(docs/dev/automation.md). A file that can't be read holds the release, in plain words.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BUILDS = "builds.jsonl"
SERVING_LOG = "serving_log.jsonl"
SERVING = "SERVING"
BUILT_TYPES = ("model", "seed", "snapshot")  # resource types that build a relation
TEST_TYPES = ("test", "unit_test")
RELEASE_TAG = re.compile(r"release-\d{4}-\d{2}-\d{2}(\.[2-9]|\.[1-9]\d+)?")


class GateInputMissing(FileNotFoundError):
    """dbt's run record is missing or unreadable, so there is nothing to judge (the gate holds)."""


class GateStateUnreadable(ValueError):
    """An ops file (SERVING or a log) can't be read; the gate holds rather than guess what is served."""


@dataclass(frozen=True)
class Build:
    build_id: str
    generated_at: str
    dbt_version: str
    invocation_id: str
    models_built: int
    model_errors: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    rule_errors: list[dict[str, str]] = field(default_factory=list)
    data_failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    tests_passed: int = 0

    @property
    def servable(self) -> bool:
        return not (
            self.model_errors or self.skipped or self.rule_errors or self.data_failures or self.incomplete
        )

    def reason(self) -> str:
        """Why the build is held, in plain words; empty when it can be served."""
        parts = list(self.incomplete)
        if self.model_errors:
            parts.append(f"{len(self.model_errors)} model(s) failed to build")
        if self.skipped:
            parts.append(
                f"{len(self.skipped)} model(s) or check(s) were skipped because something before them failed"
            )
        if self.rule_errors:
            parts.append(f"{len(self.rule_errors)} check(s) could not run")
        if self.data_failures:
            names = ", ".join(f["test"] for f in self.data_failures)
            parts.append(f"{len(self.data_failures)} error-severity check(s) failed ({names})")
        return "; ".join(parts)


def _read(path: Path) -> Mapping[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise GateInputMissing(
            f"{path}: dbt's run record could not be read ({e}); the release is held"
        ) from e
    if not isinstance(doc, Mapping):
        raise GateInputMissing(f"{path}: not a dbt run record; the release is held")
    return doc


def _kind(uid: str, node: Mapping[str, Any]) -> str:
    return str(node.get("resource_type") or uid.split(".", 1)[0])


def read_build(target: Path) -> Build:
    """Sort one dbt build (target/run_results.json and target/manifest.json) into the gate's outcomes."""
    results = _read(target / "run_results.json")
    manifest = _read(target / "manifest.json")
    nodes: dict[str, Any] = {**(manifest.get("nodes") or {}), **(manifest.get("unit_tests") or {})}
    meta = results.get("metadata") or {}
    model_errors: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    rule_errors: list[dict[str, str]] = []
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    built = passed = 0
    seen: set[str] = set()
    rows = results.get("results") or []
    for r in rows:
        uid, status = str(r.get("unique_id", "")), str(r.get("status", ""))
        seen.add(uid)
        node = nodes.get(uid) or {}
        kind = _kind(uid, node)
        name = str(node.get("name") or uid.split(".")[-1])
        message = str(r.get("message") or "")
        if status == "skipped":
            skipped.append({"node": name, "type": kind})
        elif kind in BUILT_TYPES:
            if status == "success":
                built += 1
            else:
                model_errors.append({"model": name, "type": kind, "error": message or status})
        elif kind in TEST_TYPES:
            config = node.get("config") or {}
            item = {
                "test": name,
                "type": kind,
                "severity": "error"
                if kind == "unit_test"
                else str(config.get("severity") or "error").lower(),
                "dimension": (config.get("meta") or node.get("meta") or {}).get("dimension"),
                "failures": r.get("failures"),
            }
            if status == "pass":
                passed += 1
            elif status == "warn":
                warnings.append(item)
            elif status == "fail":
                failures.append(item)
            else:  # error, or a status this gate does not know: the check did not run
                rule_errors.append({"test": name, "type": kind, "error": message or status})
        elif status != "success":  # any other resource type: fail closed on anything but success
            model_errors.append({"model": name, "type": kind, "error": message or status})
    for uid, node in nodes.items():  # a node the project declares but the run never reached
        if _kind(uid, node) in BUILT_TYPES and uid not in seen:
            model_errors.append(
                {"model": str(node.get("name") or uid), "type": _kind(uid, node), "error": "not built"}
            )
    incomplete = []
    if not rows:
        incomplete.append("dbt's run record lists nothing built or tested")
    elif not (passed or warnings or failures or rule_errors or any(s["type"] in TEST_TYPES for s in skipped)):
        incomplete.append("no test ran")
    invocation = str(meta.get("invocation_id") or "")
    generated = str(meta.get("generated_at") or "")
    digest = hashlib.sha256(f"{invocation}|{generated}".encode()).hexdigest()[:12]
    return Build(
        build_id=f"build-{digest}",
        generated_at=generated,
        dbt_version=str(meta.get("dbt_version") or ""),
        invocation_id=invocation,
        models_built=built,
        model_errors=model_errors,
        skipped=skipped,
        rule_errors=rule_errors,
        data_failures=failures,
        warnings=warnings,
        incomplete=incomplete,
        tests_passed=passed,
    )


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def serving(ops: Path) -> dict[str, Any] | None:
    """The release in service now ({release_ref, build_id, since}), or None before the first served release."""
    p = ops / SERVING
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise GateStateUnreadable(
            f"{p} can't be read ({type(e).__name__}), so the release served now is unknown"
        ) from e
    if not isinstance(doc, dict) or not isinstance(doc.get("release_ref"), str):
        raise GateStateUnreadable(f"{p} does not name a release, so the release served now is unknown")
    return doc


def read_log(ops: Path, name: str) -> list[dict[str, Any]]:
    p = ops / name
    if not p.is_file():
        return []
    try:
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as e:
        raise GateStateUnreadable(f"{p} can't be read ({type(e).__name__}); a line may be cut short") from e
    if not all(isinstance(r, dict) for r in rows):
        raise GateStateUnreadable(f"{p} has a line that is not a record")
    return rows


def _now(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


def decide(ops: Path, build: Build, *, now: datetime | None = None) -> dict[str, Any]:
    """Record the build and its verdict (pass or hold); returns the serving-log row written. SERVING is untouched:
    a passed build is served only once its upload is confirmed (confirm)."""
    at = _now(now)
    previous = serving(ops)
    read_log(ops, BUILDS)  # an unreadable log holds the release before anything is appended to it
    read_log(ops, SERVING_LOG)
    _append(ops / BUILDS, {**asdict(build), "recorded_at": at, "servable": build.servable})
    kept = previous["release_ref"] if previous else None
    if build.servable:
        row = {"at": at, "action": "pass", "build_id": build.build_id, "serving": kept,
               "warnings": len(build.warnings)}  # fmt: skip
    else:
        row = {"at": at, "action": "hold", "build_id": build.build_id, "kept": kept, "reason": build.reason()}
    _append(ops / SERVING_LOG, row)
    return row


def confirm(ops: Path, build: Build, release_ref: str, *, now: datetime | None = None) -> dict[str, Any]:
    """After the upload: record `serve` for the tag export-public created and swap SERVING to it."""
    if not RELEASE_TAG.fullmatch(release_ref):
        raise ValueError(
            f"{release_ref!r} is not a release tag (release-<run date>, or release-<run date>.N)"
        )
    if not build.servable:
        raise ValueError(
            f"build {build.build_id} did not pass the gate, so it can't be served ({build.reason()})"
        )
    passed = [
        r
        for r in read_log(ops, SERVING_LOG)
        if r.get("action") == "pass" and r.get("build_id") == build.build_id
    ]
    if not passed:
        raise ValueError(f"build {build.build_id} has no recorded pass; run the gate check first")
    at = _now(now)
    previous = serving(ops)
    row = {"at": at, "action": "serve", "release_ref": release_ref, "build_id": build.build_id,
           "previous": previous["release_ref"] if previous else None, "warnings": len(build.warnings)}  # fmt: skip
    ops.mkdir(parents=True, exist_ok=True)
    tmp = ops / f".{SERVING}.{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps({"release_ref": release_ref, "build_id": build.build_id, "since": at}) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, ops / SERVING)
    _append(ops / SERVING_LOG, row)
    return row


def _hold(ops: Path, reason: str) -> None:
    """Log a hold that has no build to record (no run record); never raises over unreadable ops state."""
    try:
        previous = serving(ops)
        kept = previous["release_ref"] if previous else None
    except GateStateUnreadable:
        kept = None
    _append(
        ops / SERVING_LOG,
        {"at": _now(None), "action": "hold", "build_id": None, "kept": kept, "reason": reason},
    )


def run(target: Path, ops: Path) -> tuple[bool, str]:
    """The CLI's check: (passed, plain-words message). Anything unreadable holds the release."""
    try:
        build = read_build(target)
    except GateInputMissing as e:
        _hold(ops, str(e))
        return False, str(e)
    try:
        row = decide(ops, build)
    except GateStateUnreadable as e:
        return False, f"The release is held: {e}. Restore the file from the ops dataset, then run again."
    if row["action"] == "pass":
        warn = f", with {len(build.warnings)} warning(s)" if build.warnings else ""
        return (
            True,
            f"Build {build.build_id} passed the gate{warn}; it is served once its release is uploaded.",
        )
    kept = row["kept"] or "no release (none was served before)"
    return False, f"Build {build.build_id} is held: {row['reason']}. Still serving {kept}."


def run_confirm(target: Path, ops: Path, release_ref: str) -> tuple[bool, str]:
    """The CLI's confirm, after the upload: (recorded, plain-words message)."""
    try:
        build = read_build(target)
        row = confirm(ops, build, release_ref)
    except (GateInputMissing, GateStateUnreadable, ValueError) as e:
        return False, f"{release_ref} was not recorded as served: {e}."
    previous = row["previous"] or "none"
    return True, f"{release_ref} is served (build {build.build_id}; before it: {previous})."

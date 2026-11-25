"""The release gate: a hub release is served only when its transform build passed every error-severity test.

After `dbt build`, the gate reads dbt's own record of the run (target/run_results.json, and target/manifest.json
for each test's severity and dimension) and sorts every node into one of four outcomes:

    model error     a model failed to build: nothing from this build can be served
    rule error      a test could not run (its SQL no longer fits the table): the rule needs a maintainer
    data failure    an error-severity test ran and failed: the data breaks a contract rule
    warning         a warn-severity test failed: served, and the warning travels with the answers

The build is served only when there is no model error, rule error or data failure. Otherwise the release
served before stays served. Either way the build is appended to ops/builds.jsonl and the decision to
ops/serving_log.jsonl, and ops/SERVING names the release in service. All three are append-only or swapped in one
step, under the lake ($HDR_LAKE/ops by default), and never in git (hard rule 5).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BUILDS = "builds.jsonl"
SERVING_LOG = "serving_log.jsonl"
SERVING = "SERVING"


class GateInputMissing(FileNotFoundError):
    """dbt's run record is missing or unreadable, so there is nothing to judge (the gate holds)."""


@dataclass(frozen=True)
class Build:
    build_id: str
    generated_at: str
    dbt_version: str
    invocation_id: str
    release_ref: str
    models_built: int
    model_errors: list[dict[str, str]] = field(default_factory=list)
    rule_errors: list[dict[str, str]] = field(default_factory=list)
    data_failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    tests_passed: int = 0

    @property
    def servable(self) -> bool:
        return not (self.model_errors or self.rule_errors or self.data_failures)

    def reason(self) -> str:
        """Why the build is held, in plain words; empty when it can be served."""
        parts = []
        if self.model_errors:
            parts.append(f"{len(self.model_errors)} model(s) failed to build")
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


def read_build(target: Path, release_ref: str) -> Build:
    """Sort one dbt build (target/run_results.json and target/manifest.json) into the gate's outcomes."""
    results = _read(target / "run_results.json")
    nodes = _read(target / "manifest.json").get("nodes") or {}
    meta = results.get("metadata") or {}
    model_errors: list[dict[str, str]] = []
    rule_errors: list[dict[str, str]] = []
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    built = passed = 0
    for r in results.get("results") or []:
        uid, status = str(r.get("unique_id", "")), str(r.get("status", ""))
        node = nodes.get(uid) or {}
        name = str(node.get("name") or uid.split(".")[-1])
        message = str(r.get("message") or "")
        if uid.startswith("model."):
            if status == "success":
                built += 1
            else:
                model_errors.append({"model": name, "error": message or status})
        elif uid.startswith("test."):
            config = node.get("config") or {}
            item = {
                "test": name,
                "severity": str(config.get("severity") or "error").lower(),
                "dimension": (config.get("meta") or node.get("meta") or {}).get("dimension"),
                "failures": r.get("failures"),
            }
            if status == "pass":
                passed += 1
            elif status == "warn":
                warnings.append(item)
            elif status == "fail":
                failures.append(item)
            else:  # error, or skipped because something it depends on failed: the check did not run
                rule_errors.append({"test": name, "error": message or status})
    invocation = str(meta.get("invocation_id") or "")
    digest = hashlib.sha256(f"{invocation}|{release_ref}".encode()).hexdigest()[:12]
    return Build(
        build_id=f"build-{digest}",
        generated_at=str(meta.get("generated_at") or ""),
        dbt_version=str(meta.get("dbt_version") or ""),
        invocation_id=invocation,
        release_ref=release_ref,
        models_built=built,
        model_errors=model_errors,
        rule_errors=rule_errors,
        data_failures=failures,
        warnings=warnings,
        tests_passed=passed,
    )


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def serving(ops: Path) -> dict[str, Any] | None:
    """The release in service now ({release_ref, build_id, since}), or None before the first served build."""
    p = ops / SERVING
    if not p.is_file():
        return None
    doc = json.loads(p.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else None


def decide(ops: Path, build: Build, *, now: datetime | None = None) -> dict[str, Any]:
    """Record the build, then serve it or keep the previous release; returns the serving-log row written."""
    at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    _append(ops / BUILDS, {**asdict(build), "recorded_at": at, "servable": build.servable})
    previous = serving(ops)
    if build.servable:
        row = {"at": at, "action": "serve", "release_ref": build.release_ref, "build_id": build.build_id,
               "previous": previous["release_ref"] if previous else None, "warnings": len(build.warnings)}  # fmt: skip
        tmp = ops / f".{SERVING}.{os.getpid()}.tmp"
        tmp.write_text(
            json.dumps({"release_ref": build.release_ref, "build_id": build.build_id, "since": at}) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, ops / SERVING)
    else:
        row = {"at": at, "action": "hold", "release_ref": build.release_ref, "build_id": build.build_id,
               "kept": previous["release_ref"] if previous else None, "reason": build.reason()}  # fmt: skip
    _append(ops / SERVING_LOG, row)
    return row


def read_log(ops: Path, name: str) -> list[dict[str, Any]]:
    p = ops / name
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(target: Path, ops: Path, release_ref: str) -> tuple[bool, str]:
    """The CLI's gate: (served, plain-words message). An unreadable run record holds the release."""
    try:
        build = read_build(target, release_ref)
    except GateInputMissing as e:
        previous = serving(ops)
        at = datetime.now(UTC).isoformat()
        _append(ops / SERVING_LOG, {"at": at, "action": "hold", "release_ref": release_ref, "build_id": None,
                                     "kept": previous["release_ref"] if previous else None, "reason": str(e)})  # fmt: skip
        return False, str(e)
    row = decide(ops, build)
    if row["action"] == "serve":
        warn = f", with {len(build.warnings)} warning(s)" if build.warnings else ""
        return True, f"{release_ref} is served (build {build.build_id}{warn})."
    kept = row["kept"] or "no release (none was served before)"
    return False, f"{release_ref} is held: {row['reason']}. Still serving {kept}."

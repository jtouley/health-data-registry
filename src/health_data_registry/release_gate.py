"""The release gate: a hub release is served only when its transform build passed every error-severity test.

After `dbt build`, the gate reads dbt's own record of the run (target/run_results.json, and target/manifest.json
for each node's resource type, severity and dimension) and sorts every result into one of these outcomes:

    model error     a model, seed or snapshot failed to build, or one the project declares has no result
    skipped         a node was skipped because something it depends on failed (reported apart, so a failed
                    check upstream is not counted as a model that failed)
    rule error      a test could not run (its SQL no longer fits the table, or it measured nothing): a metric
                    error, never counted as a data failure
    not evaluated   a value rule whose column has no values, so there was nothing to judge (its test's error
                    starts "Not evaluated"; transform/macros/odcs_check.sql): never a pass
    data failure    an error-severity test, or a unit test, ran and failed
    warning         a warn-severity test failed: served, and the warning travels with the answers
    incomplete      the run record has no results, or no test or unit test ran: nothing was judged

The gate fails closed. Any result type it does not know, with any status but success, holds the build. So does a
run record and manifest from different dbt runs (their invocation_id must match), and so does a missing model or
test: the expected set comes from the contracts (expected_from: every staging, intermediate and model-contract
model, one named test per contract quality rule, and each generated dbt-expectations check), and each expected
test must pass, or warn when its rule is warn-severity. A missing or unreadable artifact holds it too. Every pass
is kept too (Build.passed), by test and rule, so the build record says which rules qualified the data.

The build id is a content hash of the build's inputs (build_inputs: each stored dataset's sorted raw sha256s,
each contract's version and file hash, the dbt vars and the code commit), never of a snapshot id, dbt's run id,
its thread count or the clock, so the same inputs always give the same id.

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
NOT_EVALUATED = "not evaluated:"  # odcs_check's error for a value rule with nothing to judge (dbt wraps it)
RELEASE_TAG = re.compile(r"release-\d{4}-\d{2}-\d{2}(\.[2-9]|\.[1-9]\d+)?")


# An expected test missing from the run: not evaluated. Its item carries "reason": "no_result", which reason()
# reads, so rewording this text never moves a test under another label.
NO_RESULT = "expected by the contracts, no result"


class GateInputMissing(FileNotFoundError):
    """dbt's run record is missing or unreadable, so there is nothing to judge (the gate holds)."""


class GateStateUnreadable(ValueError):
    """An ops file (SERVING or a log) can't be read; the gate holds rather than guess what is served."""


@dataclass(frozen=True)
class Expected:
    """What a complete build has: model names, and test name -> the severity its contract rule declares."""

    models: frozenset[str]
    tests: Mapping[str, str]


def expected_from(registry: Any) -> Expected:
    """The models and named tests the contracts say a build must have (transform_gen names them)."""
    from health_data_registry.quality import rules_for
    from health_data_registry.transform_gen import dq_test_severities, generate, model_contracts, stored

    models = {p.stem for p in generate(registry) if p.suffix == ".sql"}
    models |= {c.id for _path, c in model_contracts(registry.root)}
    tests = {f"{ds.id}_{r.id}": r.severity for ds in stored(registry) for r in rules_for(ds.contract)}
    # The generated dbt-expectations checks: reconciliation, freshness and value ranges.
    for ds in stored(registry):
        tests |= dq_test_severities(ds)
    return Expected(frozenset(models), tests)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


DBT_PROJECT = Path("transform/dbt_project.yml")


def build_inputs(registry: Any, lake: Path, commit: str) -> dict[str, Any]:
    """Everything a build is made from, and nothing about how it ran, for its content-hash id: the code commit;
    for each stored dataset the sorted sha256s of the raw bytes its LATEST snapshot was made from (an empty list
    when the lake has none) and its contract's version and file hash; each model contract's file hash; and the dbt
    project's vars. Not the snapshot id or retrieval time: fetching identical bytes again gives the same id (the
    snapshot id stays in each row and manifest as provenance). Not dbt's run id, thread count or the clock.
    Vars passed on dbt's command line (--vars) are not read: the workflow passes none, so a run that adds some
    must add them here too. Raises GateInputMissing when a snapshot, contract or the dbt project can't be read."""
    import yaml

    from health_data_registry.store import latest_snapshot
    from health_data_registry.transform_gen import model_contracts, stored

    datasets: dict[str, Any] = {}
    try:
        for ds in stored(registry):
            found = latest_snapshot(lake, ds.id)
            datasets[ds.id] = {
                "raw_sha256": sorted(found[1].raw_sha256) if found else [],
                "contract_version": ds.contract.version,
                "contract_sha256": _sha256(registry.root / ds.entry.contract),
            }
        models = {c.id: _sha256(registry.root / p) for p, c in model_contracts(registry.root)}
        project = registry.root / DBT_PROJECT
        # A missing project can't have built anything: read_text raises, and the release is held.
        dbt_vars = (yaml.safe_load(project.read_text(encoding="utf-8")) or {}).get("vars") or {}
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise GateInputMissing(f"the build's inputs could not be read ({e}); the release is held") from e
    return {"commit": commit, "datasets": datasets, "model_contracts": models, "dbt_vars": dbt_vars}


def build_id_of(inputs: Mapping[str, Any]) -> str:
    """The same inputs always give the same id."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return "build-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


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
    not_evaluated: list[dict[str, str]] = field(default_factory=list)
    data_failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    tests_passed: int = 0
    # Every check that passed, by test name with its rule, dimension and severity (dbt's run_results.json), so an
    # answer can cite the rule that qualified it, not only the ones that failed.
    passed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def servable(self) -> bool:
        return not (
            self.model_errors
            or self.skipped
            or self.rule_errors
            or self.not_evaluated
            or self.data_failures
            or self.incomplete
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
        empty = [f["test"] for f in self.not_evaluated if f.get("reason") != "no_result"]
        if empty:
            parts.append(
                f"{len(empty)} check(s) could not be judged, having no values to check ({', '.join(empty)})"
            )
        absent = [f["test"] for f in self.not_evaluated if f.get("reason") == "no_result"]
        if absent:
            parts.append(f"{len(absent)} check(s) the contracts expect have no result ({', '.join(absent)})")
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


def read_build(
    target: Path, *, expected: Expected | None = None, inputs: Mapping[str, Any] | None = None
) -> Build:
    """Sort one dbt build (target/run_results.json and target/manifest.json) into the gate's outcomes.

    With `expected`, every expected model must have built and every expected test must have passed (or warned,
    for a warn-severity rule). With `inputs`, the build id is their content hash (build_id_of); without, it is a
    hash of the run record (tests only).
    """
    results = _read(target / "run_results.json")
    manifest = _read(target / "manifest.json")
    nodes: dict[str, Any] = {**(manifest.get("nodes") or {}), **(manifest.get("unit_tests") or {})}
    meta = results.get("metadata") or {}
    model_errors: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    rule_errors: list[dict[str, str]] = []
    unjudged: list[dict[str, str]] = []
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
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
            test_meta = config.get("meta") or node.get("meta") or {}
            item = {
                "test": name,
                "type": kind,
                "severity": "error"
                if kind == "unit_test"
                else str(config.get("severity") or "error").lower(),
                "dimension": test_meta.get("dimension"),
                # Every result names its contract rule, so a pass, a warning and a failure join to it alike.
                "rule": test_meta.get("rule"),
                "failures": r.get("failures"),
            }
            if status == "pass":
                passed += 1
                passes.append({k: v for k, v in item.items() if k != "failures"})
            elif status == "warn":
                warnings.append(item)
            elif status == "fail":
                failures.append(item)
            elif status == "error" and NOT_EVALUATED in message.lower():
                # As the read path (QualityReport.blocking): not evaluated holds the build for an error-severity
                # rule and is a warning for a warn-severity one. The contracts' severity decides when the test
                # is expected, so a run whose config downgraded the rule can't turn a hold into a warning.
                severity = (expected.tests.get(name) if expected else None) or item["severity"]
                if severity == "warn":
                    warnings.append({**item, "error": message})
                else:
                    unjudged.append({"test": name, "type": kind, "error": message})
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
    elif not (
        passed
        or warnings
        or failures
        or rule_errors
        or unjudged
        or any(s["type"] in TEST_TYPES for s in skipped)
    ):
        incomplete.append("no test ran")
    invocation = str(meta.get("invocation_id") or "")
    built_by = str((manifest.get("metadata") or {}).get("invocation_id") or "")
    if not invocation or invocation != built_by:
        incomplete.append(
            "dbt's run record and its manifest come from different runs (or name none), so they don't describe "
            "one build"
        )
    if expected is not None:
        _check_expected(expected, rows, nodes, model_errors, unjudged, failures)
    generated = str(meta.get("generated_at") or "")
    if inputs is not None:
        build_id = build_id_of(inputs)
    else:
        build_id = "build-" + hashlib.sha256(f"{invocation}|{generated}".encode()).hexdigest()[:12]
    return Build(
        build_id=build_id,
        generated_at=generated,
        dbt_version=str(meta.get("dbt_version") or ""),
        invocation_id=invocation,
        models_built=built,
        model_errors=model_errors,
        skipped=skipped,
        rule_errors=rule_errors,
        not_evaluated=unjudged,
        data_failures=failures,
        warnings=warnings,
        incomplete=incomplete,
        tests_passed=passed,
        passed=passes,
    )


def _check_expected(
    expected: Expected,
    rows: list[Any],
    nodes: Mapping[str, Any],
    model_errors: list[dict[str, str]],
    unjudged: list[dict[str, str]],
    failures: list[dict[str, Any]],
) -> None:
    """Every model and test the contracts expect must be in the run record with an acceptable status. An expected
    test with no result was never evaluated (issue #222): it holds the release under that label, apart from a
    rule that ran and errored and from a data failure."""
    status: dict[str, str] = {}
    for r in rows:
        uid = str(r.get("unique_id", ""))
        status[str((nodes.get(uid) or {}).get("name") or uid.split(".")[-1])] = str(r.get("status", ""))
    for name in sorted(expected.models):
        if name not in status:
            model_errors.append(
                {"model": name, "type": "model", "error": "expected by the contracts, not built"}
            )
    for name, severity in sorted(expected.tests.items()):
        got = status.get(name)
        if got is None:
            unjudged.append({"test": name, "type": "test", "error": NO_RESULT, "reason": "no_result"})
        elif got == "warn" and severity != "warn":  # the run downgraded an error-severity rule
            failures.append(
                {"test": name, "type": "test", "severity": severity, "dimension": None, "failures": None}
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


def decide(
    ops: Path, build: Build, *, now: datetime | None = None, run_date: str | None = None
) -> dict[str, Any]:
    """Record the build and its verdict (pass or hold); returns the serving-log row written. SERVING is untouched:
    a passed build is served only once its upload is confirmed (confirm). A pass records the dbt run it judged
    and the run date its release must carry, so confirm promotes exactly that build and release."""
    at = _now(now)
    previous = serving(ops)
    read_log(ops, BUILDS)  # an unreadable log holds the release before anything is appended to it
    read_log(ops, SERVING_LOG)
    _append(ops / BUILDS, {**asdict(build), "recorded_at": at, "servable": build.servable})
    kept = previous["release_ref"] if previous else None
    if build.servable:
        row = {"at": at, "action": "pass", "build_id": build.build_id, "serving": kept,
               "warnings": len(build.warnings), "invocation_id": build.invocation_id,
               "run_date": run_date}  # fmt: skip
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
    last = passed[-1]
    if last.get("invocation_id") != build.invocation_id:
        raise ValueError(
            f"build {build.build_id} passed in dbt run {last.get('invocation_id')}, but this run record is from "
            f"{build.invocation_id}; only the run that was judged can be served"
        )
    run_date = last.get("run_date")
    if not run_date:  # a pass with no run date could be served under any date's tag
        raise ValueError(
            f"build {build.build_id} passed without a run date, so no release tag can be matched to it; run the "
            "gate check again with --run-date"
        )
    if not release_ref.startswith(f"release-{run_date}"):
        raise ValueError(
            f"{release_ref} is not a release of run date {run_date}, the run build {build.build_id} passed for"
        )
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


def run(
    target: Path,
    ops: Path,
    *,
    expected: Expected | None = None,
    inputs: Mapping[str, Any] | None = None,
    run_date: str | None = None,
) -> tuple[bool, str]:
    """The CLI's check: (passed, plain-words message). Anything unreadable holds the release."""
    try:
        build = read_build(target, expected=expected, inputs=inputs)
    except GateInputMissing as e:
        _hold(ops, str(e))
        return False, str(e)
    try:
        row = decide(ops, build, run_date=run_date)
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


def run_confirm(
    target: Path,
    ops: Path,
    release_ref: str,
    *,
    expected: Expected | None = None,
    inputs: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """The CLI's confirm, after the upload: (recorded, plain-words message)."""
    try:
        build = read_build(target, expected=expected, inputs=inputs)

        row = confirm(ops, build, release_ref)
    except (GateInputMissing, GateStateUnreadable, ValueError) as e:
        return False, f"{release_ref} was not recorded as served: {e}."
    previous = row["previous"] or "none"
    return True, f"{release_ref} is served (build {build.build_id}; before it: {previous})."

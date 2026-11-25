"""scripts/verify_release_tag.py: a release ships only from a tag on main that passed CI, against a fake API."""

from __future__ import annotations

import importlib.util
import os
import sys
import urllib.parse
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/registry"
TAG = "v1.2.3"
SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG_OBJ = "c" * 40
# .github/ and space/ are not copied into mutants/, where mutmut runs the suite.
reads_workflows = pytest.mark.skipif(
    "MUTANT_UNDER_TEST" in os.environ, reason=".github/, space/ not in mutants/"
)


@pytest.fixture
def vr(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_release_tag", ROOT / "scripts" / "verify_release_tag.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "verify_release_tag", mod)
    spec.loader.exec_module(mod)
    return mod


def _run(**over: Any) -> dict[str, Any]:
    run: dict[str, Any] = {
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
    }
    run.update(over)
    return run


class FakeApi:
    """Answers the four GitHub API paths the check uses; records every path asked."""

    def __init__(
        self,
        *,
        ref: dict[str, Any] | None = None,
        tag_objects: dict[str, dict[str, Any]] | None = None,
        compare: str = "behind",
        runs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.ref = {"type": "commit", "sha": SHA} if ref is None else ref
        self.tag_objects = tag_objects or {}
        self.compare = compare
        self.runs = [_run()] if runs is None else runs
        self.paths: list[str] = []

    def __call__(self, path: str) -> Any:
        self.paths.append(path)
        url = urllib.parse.urlsplit(path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == f"/repos/{REPO}/git/ref/tags/{TAG}":
            if not self.ref:
                raise KeyError(path)
            return {"object": self.ref}
        if url.path.startswith(f"/repos/{REPO}/git/tags/"):
            return {"object": self.tag_objects[url.path.rsplit("/", 1)[1]]}
        if url.path.startswith(f"/repos/{REPO}/compare/main..."):
            return {"status": self.compare}
        if url.path == f"/repos/{REPO}/actions/workflows/ci.yml/runs":
            assert q["branch"] == ["main"] and q["event"] == ["push"]
            per_page, page = int(q["per_page"][0]), int(q["page"][0])
            return {"workflow_runs": self.runs[(page - 1) * per_page : page * per_page]}
        raise AssertionError(f"unexpected API path {path}")


def test_a_tag_on_main_that_passed_ci_is_released(vr: ModuleType) -> None:
    api = FakeApi()
    assert vr.verify(api, REPO, TAG, SHA) == SHA
    assert f"/repos/{REPO}/compare/main...{SHA}" in api.paths


def test_without_a_sha_the_tags_commit_is_checked_and_returned(vr: ModuleType) -> None:
    assert vr.verify(FakeApi(), REPO, TAG) == SHA


@pytest.mark.parametrize("status", ["identical", "behind"])
def test_main_head_or_an_ancestor_counts_as_on_main(vr: ModuleType, status: str) -> None:
    assert vr.verify(FakeApi(compare=status), REPO, TAG) == SHA


@pytest.mark.parametrize("status", ["ahead", "diverged", None])
def test_a_commit_not_on_main_is_refused(vr: ModuleType, status: str | None) -> None:
    with pytest.raises(vr.Refused, match="not on main. Merge the change through a pull request"):
        vr.verify(FakeApi(compare=status), REPO, TAG)  # type: ignore[arg-type]


def test_an_annotated_tag_is_followed_to_its_commit(vr: ModuleType) -> None:
    api = FakeApi(ref={"type": "tag", "sha": TAG_OBJ}, tag_objects={TAG_OBJ: {"type": "commit", "sha": SHA}})
    assert vr.verify(api, REPO, TAG, SHA) == SHA


def test_a_tag_of_a_tag_is_followed_too(vr: ModuleType) -> None:
    inner = "d" * 40
    api = FakeApi(
        ref={"type": "tag", "sha": TAG_OBJ},
        tag_objects={TAG_OBJ: {"type": "tag", "sha": inner}, inner: {"type": "commit", "sha": SHA}},
    )
    assert vr.tag_commit(api, REPO, TAG) == SHA


def test_a_tag_loop_ends_in_a_refusal(vr: ModuleType) -> None:
    api = FakeApi(ref={"type": "tag", "sha": TAG_OBJ}, tag_objects={TAG_OBJ: {"type": "tag", "sha": TAG_OBJ}})
    with pytest.raises(vr.Refused, match="does not name a commit"):
        vr.tag_commit(api, REPO, TAG)
    assert len([p for p in api.paths if "/git/tags/" in p]) == vr.MAX_TAG_HOPS


@pytest.mark.parametrize(
    "ref", [{"type": "tree", "sha": SHA}, {"type": "commit", "sha": "not-a-sha"}, {"type": "commit"}]
)
def test_a_tag_on_anything_but_a_commit_is_refused(vr: ModuleType, ref: dict[str, Any]) -> None:
    with pytest.raises(vr.Refused, match="does not name a commit"):
        vr.tag_commit(FakeApi(ref=ref), REPO, TAG)


def test_a_missing_tag_is_refused_in_plain_words(vr: ModuleType) -> None:
    with pytest.raises(vr.Refused, match=f"There is no tag {TAG} in {REPO}. Push the tag first"):
        vr.verify(FakeApi(ref={}), REPO, TAG)


def test_a_tag_moved_since_the_run_started_is_refused(vr: ModuleType) -> None:
    api = FakeApi(ref={"type": "commit", "sha": OTHER_SHA})
    with pytest.raises(vr.Refused, match=f"now names {OTHER_SHA}, but this run was started for {SHA}"):
        vr.verify(api, REPO, TAG, SHA)
    assert not any("/compare/" in p for p in api.paths)


@pytest.mark.parametrize(
    "over",
    [
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"conclusion": None, "status": "in_progress"},
        {"status": "queued"},
        {"head_sha": OTHER_SHA},
        {"head_branch": "feature"},
        {"event": "pull_request"},
        {"path": ".github/workflows/other.yml"},
        {"repository": {"full_name": "fork/registry"}},
        {"head_repository": {"full_name": "fork/registry"}},
        {"repository": None},
        {"head_repository": None},
    ],
)
def test_only_a_green_push_run_of_ci_on_main_here_counts(vr: ModuleType, over: dict[str, Any]) -> None:
    with pytest.raises(vr.Refused, match=f"CI has not passed on main for {SHA}"):
        vr.verify(FakeApi(runs=[_run(**over)]), REPO, TAG)


def test_no_ci_runs_is_a_refusal(vr: ModuleType) -> None:
    with pytest.raises(vr.Refused, match="Wait for CI on main to finish green"):
        vr.verify(FakeApi(runs=[]), REPO, TAG)


def test_a_green_run_past_a_red_one_counts(vr: ModuleType) -> None:
    assert vr.verify(FakeApi(runs=[_run(conclusion="failure"), _run()]), REPO, TAG) == SHA


def test_runs_are_paged_and_paging_stops_at_the_cap(vr: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vr, "PER_PAGE", 2)
    api = FakeApi(runs=[_run(conclusion="failure")] * 3 + [_run()])
    assert vr.ci_passed_on_main(api, REPO, SHA)
    assert len([p for p in api.paths if "/runs?" in p]) == 2
    monkeypatch.setattr(vr, "MAX_PAGES", 1)
    api = FakeApi(runs=[_run(conclusion="failure")] * 2 + [_run()])
    assert not vr.ci_passed_on_main(api, REPO, SHA)
    assert len([p for p in api.paths if "/runs?" in p]) == 1


@pytest.mark.parametrize(
    ("repo", "tag", "sha", "message"),
    [
        ("not a repo", TAG, "", "is not a repository name"),
        (REPO, "1.2.3", "", "is not a release tag. Release tags look like v1.2.3"),
        (REPO, "v1.2", "", "is not a release tag"),
        (REPO, "v1.2.3-rc1", "", "is not a release tag"),
        (REPO, "release-2026-11-24", "", "is not a release tag"),
        (REPO, TAG, "abc", "is not a full commit SHA"),
        (REPO, TAG, SHA.upper(), "is not a full commit SHA"),
    ],
)
def test_bad_arguments_are_refused_before_any_api_call(
    vr: ModuleType, repo: str, tag: str, sha: str, message: str
) -> None:
    api = FakeApi()
    with pytest.raises(vr.Refused, match=message):
        vr.verify(api, repo, tag, sha)
    assert api.paths == []


def test_main_writes_the_commit_for_later_jobs(
    vr: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "github_output"
    out.write_text("earlier=1\n", encoding="utf-8")
    argv = ["--repo", REPO, "--tag", TAG, "--sha", SHA, "--github-output", str(out)]
    assert vr.main(argv, get_json=FakeApi()) == 0
    assert out.read_text(encoding="utf-8") == f"earlier=1\nsha={SHA}\n"
    assert f"{TAG} ({SHA}) is on main and passed CI there." in capsys.readouterr().out


def test_main_refuses_with_a_plain_error_and_writes_nothing(
    vr: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "github_output"
    argv = ["--repo", REPO, "--tag", TAG, "--github-output", str(out)]
    assert vr.main(argv, get_json=FakeApi(compare="ahead")) == 1
    assert not out.exists()
    assert capsys.readouterr().out.startswith(
        f"::error::Not released: Tag {TAG} names {SHA}, which is not on main"
    )


def test_an_api_error_is_a_refusal_never_a_pass(vr: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(path: str) -> Any:
        raise OSError("connection reset")

    assert vr.main(["--repo", REPO, "--tag", TAG], get_json=broken) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"::error::Not released: GitHub could not be asked whether {TAG} is on main")
    assert "(OSError)" in out and "Re-run the workflow" in out


def test_a_missing_token_is_a_refusal(
    vr: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert vr.main(["--repo", REPO, "--tag", TAG]) == 1
    assert "::error::Not released: No GITHUB_TOKEN" in capsys.readouterr().out


def test_the_api_url_must_be_https(vr: ModuleType) -> None:
    with pytest.raises(ValueError, match="must be https"):
        vr.github_get_json("http://api.github.com", "t")
    assert callable(vr.github_get_json("https://api.github.com/", "t"))


def test_a_404_from_the_api_is_a_key_error(vr: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request

    seen: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> Any:
        seen.append((request.full_url, request.headers, timeout))
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    get = vr.github_get_json("https://api.example/", "tok")
    with pytest.raises(KeyError):
        get("/repos/x/y")
    url, headers, timeout = seen[0]
    assert url == "https://api.example/repos/x/y" and timeout == 30
    assert headers["Authorization"] == "Bearer tok"

    def server_error(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, 500, "Server Error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", server_error)
    with pytest.raises(urllib.error.HTTPError):
        get("/repos/x/y")


# --- the workflows that ship a release all call the one check first ------------------------------------------------


def _workflow(name: str) -> dict[str, Any]:
    text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    assert "pull_request_target" not in text
    data: dict[str, Any] = yaml.safe_load(text)
    return data


VERIFY = "./.github/workflows/verify-release-tag.yml"


@reads_workflows
def test_the_shared_check_runs_the_script_with_least_privilege() -> None:
    wf = _workflow("verify-release-tag.yml")
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["verify"]
    assert job["permissions"] == {"actions": "read", "contents": "read"}
    step = next(s for s in job["steps"] if s.get("id") == "check")
    assert "${{" not in step["run"]  # inputs reach the shell only through env, quoted
    assert "scripts/verify_release_tag.py" in step["run"]
    assert step["env"]["TAG"] == "${{ inputs.tag }}" and step["env"]["SHA"] == "${{ inputs.sha }}"
    assert wf[True]["workflow_call"]["outputs"]["sha"]["value"] == "${{ jobs.verify.outputs.sha }}"


@reads_workflows
@pytest.mark.parametrize(("name", "first"), [("release.yml", "build"), ("npm-publish.yml", "test")])
def test_pypi_and_npm_check_the_tag_before_anything_else(name: str, first: str) -> None:
    jobs = _workflow(name)["jobs"]
    verify = jobs["verify"]
    assert verify["uses"] == VERIFY
    assert verify["with"] == {"tag": "${{ github.ref_name }}", "sha": "${{ github.sha }}"}
    assert verify["permissions"] == {"actions": "read", "contents": "read"}
    assert jobs[first]["needs"] == "verify"
    # Every other job runs after the first, directly or through another job, so nothing ships unchecked.
    for job_id, job in jobs.items():
        if job_id not in ("verify", first):
            assert job.get("needs"), job_id


@reads_workflows
def test_the_space_deploys_only_after_a_successful_release_from_its_signed_files() -> None:
    wf = _workflow("deploy-space.yml")
    on = wf[True]  # PyYAML reads the bare key `on` as True
    assert on["workflow_run"] == {"workflows": ["Release"], "types": ["completed"]}
    assert set(on) == {"workflow_run", "workflow_dispatch"}
    jobs = wf["jobs"]
    verify = jobs["verify"]
    assert verify["uses"] == VERIFY
    assert "github.event.workflow_run.conclusion == 'success'" in verify["if"]
    assert "github.event.workflow_run.event == 'push'" in verify["if"]
    assert "head_repository.full_name == github.repository" in verify["if"]
    assert "github.ref == 'refs/heads/main'" in verify["if"]
    assert (
        verify["with"]["sha"]
        == "${{ github.event_name == 'workflow_run' && github.event.workflow_run.head_sha || '' }}"
    )
    assert jobs["artifacts"]["needs"] == "verify"
    assert jobs["deploy"]["needs"] == ["verify", "artifacts"]
    assert jobs["verify-live"]["needs"] == ["artifacts", "deploy"]
    # Only the upload job is in the `space` environment, and only its upload step sees the token.
    assert [j for j, job in jobs.items() if "environment" in job] == ["deploy"]
    text = (ROOT / ".github" / "workflows" / "deploy-space.yml").read_text(encoding="utf-8")
    assert text.count("secrets.HF_TOKEN") == 2  # the token check and the upload step, nowhere else
    for step in jobs["deploy"]["steps"]:
        if "HF_TOKEN" in (step.get("env") or {}):
            assert step["id"] == "upload"
    # The artifacts job checks both signatures against release.yml at this tag, and the wheel against PyPI.
    runs = "\n".join(s.get("run", "") for s in jobs["artifacts"]["steps"])
    assert "sigstore verify github" in runs and ".github/workflows/release.yml@refs/tags/" in runs
    assert '--sha "$SHA"' in runs and "--trigger push" in runs
    assert "https://pypi.org/pypi/health-data-registry/" in runs
    assert "uv build" not in runs and "docker" not in runs
    live = jobs["verify-live"]["steps"][-1]
    assert "scripts/check_space.py" in live["run"] and "${{" not in live["run"]


@reads_workflows
def test_every_action_in_the_release_workflows_is_pinned_by_sha() -> None:
    for name in ("verify-release-tag.yml", "deploy-space.yml", "npm-publish.yml"):
        for job in _workflow(name)["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    ref = step["uses"].split("@", 1)[1]
                    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), step["uses"]


@reads_workflows
def test_the_space_image_installs_only_the_hashed_release_files() -> None:
    dockerfile = (ROOT / "space" / "Dockerfile").read_text(encoding="utf-8")
    for arg in ("HDR_TAG", "HDR_COMMIT", "HDR_WHEEL_SHA256", "HDR_REQUIREMENTS_SHA256"):
        assert f"\nARG {arg}=\n" in dockerfile  # empty in git; the deploy fills each in
    assert "sha256sum --strict -c -" in dockerfile
    assert "--require-hashes --no-deps -r" in dockerfile
    assert "git fetch" not in dockerfile and "uv sync" not in dockerfile

"""The nightly mutation run (.github/workflows/mutation.yml): its base, its survivor issues, and the release gate.

Usage:
  python scripts/mutation_nightly.py base --repo OWNER/NAME [--input-base SHA] [--github-output FILE]
  python scripts/mutation_nightly.py issues --repo OWNER/NAME --reports DIR --base SHA --run-url URL [--dry-run]
  python scripts/mutation_nightly.py gate --repo OWNER/NAME --sha SHA

base: the commit the run mutates from: the head commit of the last green nightly run on main, so each run covers
every function changed on main since then. With none yet, the commit that added the workflow (mutation tests ran
on every pull request before it). A dispatch may pass --input-base to go further back, never forward: it must be an
ancestor of the computed base, so no change is skipped. A red run never moves the base, so the next run covers its
range again.

issues: one open issue per originating pull request (or commit, when its subject names no PR), labelled
`mutation-survivors`, listing the mutants nothing killed and linking the run. A survivor belongs to the newest
commit since the base that changed a line of its function (git blame), or else to the newest one that changed its
file. An open issue with the same title is updated; a closed one that already lists every survivor is left
closed (explained); otherwise a new issue opens. Kill a survivor with a test, or explain in the issue why the
mutant is equivalent, then close it.

gate (the release gate, hard rule 13): passes only when the latest completed nightly run on main whose commit is
the released commit or an ancestor of it is green, and no file it mutates (src/health_data_registry, eval) changed
between that commit and the released one; and when no `mutation-survivors` issue is open. Any doubt, an API error
included, fails: the release waits, nothing is published.

The token is the run's own GITHUB_TOKEN (scripts/_github_api.py: https only, never sent on a redirect).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _github_api  # noqa: E402 - sibling scripts, found through the line above
import mutate_changed as mc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/mutation.yml"
WORKFLOW_FILE = "mutation.yml"
LABEL = "mutation-survivors"
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")
RUN_URL = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$")
PR_IN_SUBJECT = re.compile(r"\(#(?P<pr>[0-9]+)\)\s*$")
MUTATED_DIRS = mc.PACKAGE_DIRS
PER_PAGE = 100


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _is_ancestor(older: str, newer: str) -> bool:
    return (
        subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=ROOT, check=False).returncode
        == 0
    )


# base ------------------------------------------------------------------------------------------------------------


def last_green_sha(get: _github_api.GetJson, repo: str) -> str | None:
    """The head commit of the newest successful nightly run on main, or None."""
    query = urllib.parse.urlencode({"branch": "main", "status": "success", "per_page": PER_PAGE})
    runs = (get(f"/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?{query}") or {}).get(
        "workflow_runs"
    ) or []
    for run in runs:  # newest first
        sha = str(run.get("head_sha", ""))
        if (
            run.get("conclusion") == "success"
            and run.get("path", WORKFLOW) == WORKFLOW
            and SHA.fullmatch(sha)
        ):
            return sha
    return None


def first_base() -> str:
    """The commit that added the nightly workflow: before it, every pull request ran its own mutation tests."""
    added = _git("log", "--diff-filter=A", "--format=%H", "--", WORKFLOW).split()
    if not added:
        raise ValueError(f"{WORKFLOW} has no commit that added it in this checkout (fetch the full history)")
    return added[-1]


def choose_base(green: str | None, input_base: str, head: str) -> tuple[str, str]:
    """(base, why). The last green run's commit when it is an ancestor of `head`, else the workflow's first commit;
    an `input_base` only when it goes further back than that."""
    if green and _is_ancestor(green, head):
        base, why = green, f"the last green nightly run's commit {green}"
    else:
        base = first_base()
        why = f"the commit that added {WORKFLOW} ({base}); no green nightly run on main's history yet"
    if input_base:
        if not SHA.fullmatch(input_base):
            raise ValueError(f"--input-base is not a full commit SHA: {input_base!r}")
        if not _is_ancestor(input_base, base):
            raise ValueError(
                f"--input-base {input_base} is not an ancestor of {base} ({why}); a dispatch may only widen the"
                " range, so no change goes untested"
            )
        base, why = input_base, f"the dispatch's base {input_base}, further back than {why}"
    return base, why


# issues ----------------------------------------------------------------------------------------------------------


def survivor_function(name: str, modules: list[str]) -> tuple[str, str] | None:
    """(module, mutmut key) of a survivor `<module>.<key>__mutmut_<n>`, for the module of its shard."""
    for module in sorted(modules, key=len, reverse=True):
        key = mc.mutant_key(name, module)
        if key:
            return module, key
    return None


def blamed_commits(path: str, spans: list[tuple[int, int]], base: str) -> set[str]:
    """The commits since `base` that last changed a line in `spans` of `path` at HEAD."""
    out: set[str] = set()
    for start, end in spans:
        blame = _git("blame", "--porcelain", "-L", f"{start},{end}", f"{base}..HEAD", "--", path)
        boundary: set[str] = set()
        current = ""
        for line in blame.splitlines():
            head = line.split(" ", 1)[0]
            if SHA.fullmatch(head):
                current = head
                out.add(current)
            elif line == "boundary" and current:
                boundary.add(current)
        out -= boundary
    return out


def origin_of(path: str, spans: list[tuple[int, int]], base: str) -> str:
    """The newest commit since `base` that changed the function, else its file. '' when neither changed."""
    order = _git("rev-list", f"{base}..HEAD").split()  # newest first
    blamed = blamed_commits(path, spans, base) if spans else set()
    candidates = blamed or set(_git("rev-list", f"{base}..HEAD", "--", path).split())
    return next((c for c in order if c in candidates), "")


def origin_title(commit: str) -> str:
    subject = _git("log", "-1", "--format=%s", commit).strip()
    m = PR_IN_SUBJECT.search(subject)
    return f"Mutation survivors from #{m['pr']}" if m else f"Mutation survivors from commit {commit[:12]}"


def group_survivors(reports: list[mc.ShardResult], base: str) -> dict[str, list[str]]:
    """Issue title -> the survivors it lists, sorted."""
    out: dict[str, set[str]] = {}
    spans_by_module: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for r in reports:
        modules = sorted({mc.parse_shard_id(s)[0] for s in r.modules})
        for name in r.survivors:
            found = survivor_function(name, modules)
            commit = ""
            if found:
                module, key = found
                path = mc.module_path(module).relative_to(ROOT).as_posix()
                if module not in spans_by_module:
                    spans_by_module[module] = mc.function_spans(mc.module_path(module).read_text("utf-8"))
                commit = origin_of(path, spans_by_module[module].get(key, []), base)
            title = origin_title(commit) if commit else "Mutation survivors (origin not found)"
            out.setdefault(title, set()).add(name)
    return {t: sorted(names) for t, names in sorted(out.items())}


def issue_body(survivors: list[str], run_url: str, base: str) -> str:
    lines = "\n".join(f"- [ ] `{s}`" for s in survivors)
    return (
        f"The nightly mutation run {run_url} found mutants that no test killed in code changed since {base[:12]}"
        " (scripts/mutation_nightly.py; hard rule 13).\n\n"
        f"{lines}\n\n"
        "For each one, add or sharpen a test that fails on it, or explain in a comment why the mutant is"
        " equivalent. Inspect one with `uv run mutmut show <name>` after `make mutate`. Close this issue when every"
        " survivor is killed or explained: a release waits while it is open.\n\n"
        "---\n_Opened by the nightly mutation run (.github/workflows/mutation.yml)._"
    )


def sync_issues(
    get: _github_api.GetJson,
    send: _github_api.SendJson,
    repo: str,
    grouped: dict[str, list[str]],
    run_url: str,
    base: str,
) -> list[str]:
    """Open or update one issue per title; returns one line per action."""
    query = urllib.parse.urlencode({"labels": LABEL, "state": "all", "per_page": PER_PAGE})
    existing = [i for i in (get(f"/repos/{repo}/issues?{query}") or []) if "pull_request" not in i]
    done = []
    for title, survivors in grouped.items():
        body = issue_body(survivors, run_url, base)
        same = [i for i in existing if i.get("title") == title]
        open_ = next((i for i in same if i.get("state") == "open"), None)
        if open_ is not None:
            send("PATCH", f"/repos/{repo}/issues/{int(open_['number'])}", {"body": body})
            done.append(f"updated #{open_['number']}: {title}")
        elif any(all(f"`{s}`" in str(i.get("body") or "") for s in survivors) for i in same):
            done.append(f"left closed (already explained): {title}")
        else:
            made = send("POST", f"/repos/{repo}/issues", {"title": title, "body": body, "labels": [LABEL]})
            done.append(f"opened #{(made or {}).get('number', '?')}: {title}")
    return done


# gate ------------------------------------------------------------------------------------------------------------


def gate(get: _github_api.GetJson, repo: str, sha: str) -> tuple[bool, list[str]]:
    """Whether a release of `sha` may go ahead (the module docstring), and why."""
    query = urllib.parse.urlencode({"branch": "main", "status": "completed", "per_page": PER_PAGE})
    runs = (get(f"/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?{query}") or {}).get(
        "workflow_runs"
    ) or []
    covering = None
    for run in runs:  # newest first
        head = str(run.get("head_sha", ""))
        if not SHA.fullmatch(head) or run.get("path", WORKFLOW) != WORKFLOW:
            continue
        if head == sha:
            covering = (run, [])
            break
        compare = get(f"/repos/{repo}/compare/{head}...{sha}") or {}
        if compare.get("status") not in ("ahead", "identical"):
            continue  # not an ancestor of the released commit
        files = compare.get("files")
        if not isinstance(files, list) or len(files) >= 300:  # the compare API lists at most 300
            return False, [f"Could not tell what changed between {head} and {sha}; release blocked."]
        mutated = [
            str(f.get("filename"))
            for f in files
            if str(f.get("filename", "")).startswith(tuple(d + "/" for d in MUTATED_DIRS))
            or str(f.get("previous_filename", "")).startswith(tuple(d + "/" for d in MUTATED_DIRS))
        ]
        covering = (run, mutated)
        break
    if covering is None:
        return False, [f"No completed nightly mutation run on main covers {sha}; run it (Actions, Mutation)."]
    run, mutated = covering
    url = run.get("html_url", f"run {run.get('id')}")
    lines = []
    ok = True
    if run.get("conclusion") != "success":
        ok = False
        lines.append(
            f"The latest nightly mutation run on this commit's history ended '{run.get('conclusion')}': {url}"
        )
    if mutated:
        ok = False
        lines.append(
            f"Mutated code changed after the latest nightly run ({url}): {', '.join(sorted(mutated)[:10])}. Wait"
            " for tonight's run, or start it by hand (Actions, Mutation, Run workflow)."
        )
    query = urllib.parse.urlencode({"labels": LABEL, "state": "open", "per_page": PER_PAGE})
    open_ = [i for i in (get(f"/repos/{repo}/issues?{query}") or []) if "pull_request" not in i]
    if open_:
        ok = False
        lines += [f"Open survivor issue #{i.get('number')}: {i.get('title')}" for i in open_]
    if ok:
        lines.append(f"Mutation gate passed: {url} is green, covers {sha}, and no survivor issue is open.")
    return ok, lines


# main ------------------------------------------------------------------------------------------------------------


def _token_get_send() -> tuple[_github_api.GetJson, _github_api.SendJson]:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise ValueError("GITHUB_TOKEN is not set")
    return _github_api.get_json(token), _github_api.send_json(token)


def main(
    argv: list[str] | None = None,
    get: _github_api.GetJson | None = None,
    send: _github_api.SendJson | None = None,
) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("base")
    b.add_argument("--repo", required=True)
    b.add_argument("--input-base", default="")
    b.add_argument("--github-output", type=Path)
    i = sub.add_parser("issues")
    i.add_argument("--repo", required=True)
    i.add_argument("--reports", type=Path, required=True)
    i.add_argument("--base", required=True)
    i.add_argument("--run-url", required=True)
    i.add_argument("--dry-run", action="store_true", help="print the issues, change nothing")
    g = sub.add_parser("gate")
    g.add_argument("--repo", required=True)
    g.add_argument("--sha", required=True)
    a = ap.parse_args(argv)
    if not REPO.fullmatch(a.repo):
        ap.error(f"not a repository name: {a.repo!r}")
    try:
        if get is None or send is None:
            get, send = _token_get_send()
        if a.cmd == "base":
            head = _git("rev-parse", "HEAD").strip()
            base, why = choose_base(last_green_sha(get, a.repo), a.input_base, head)
            print(f"Mutating what changed on main since {why}.", file=sys.stderr)
            print(base)
            if a.github_output:
                with a.github_output.open("a", encoding="utf-8") as f:
                    f.write(f"base={base}\n")
            return 0
        if a.cmd == "issues":
            if not SHA.fullmatch(a.base) or not RUN_URL.fullmatch(a.run_url):
                ap.error("--base must be a full commit SHA and --run-url an Actions run URL")
            grouped = group_survivors(mc.load_reports(a.reports), a.base)
            if not grouped:
                print("No survivors: no issue to open.")
                return 0
            if a.dry_run:
                for title, names in grouped.items():
                    print(f"{title}: {len(names)} survivor(s)")
                return 0
            print("\n".join(sync_issues(get, send, a.repo, grouped, a.run_url, a.base)))
            return 0
        if not SHA.fullmatch(a.sha):
            ap.error(f"--sha is not a full commit SHA: {a.sha!r}")
        ok, lines = gate(get, a.repo, a.sha)
        print("\n".join(lines))
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001 (fail closed, in plain words)
        print(f"::error::The nightly mutation step could not finish ({type(e).__name__}: {e}).")
        return 1


if __name__ == "__main__":
    sys.exit(main())

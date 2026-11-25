# Branch hygiene: archive tags and finished branches

[`branch-hygiene.yml`](../../.github/workflows/branch-hygiene.yml) keeps the branch list short without losing
work. Before it deletes a branch, it creates an archive tag and checks it. It runs in Actions with `GITHUB_TOKEN`,
because a Claude session can push only to `refs/heads`: the session's git proxy refuses tags, other refs and
deletes.

[`scripts/branch_hygiene.py`](../../scripts/branch_hygiene.py) makes every decision, from the GitHub API and
[`config/branch_keep.yaml`](../../config/branch_keep.yaml). It is unit-tested against a fake GitHub API
(`tests/test_branch_hygiene.py`) and mutation-tested in CI. Issue #67 is the spec. The workflow is live from its
first run; there is no trial week.

## When it runs

| Trigger | Job | What it does |
|---|---|---|
| Daily at 03:41 UTC | `sweep` | Archives finished and inactive branches and purges the `purge` list, then posts the list to the tracking issue |
| A pull request from this repository closes | `pr-closed` | Tags the head if it merged, and archives the pull request's `pre-pr/<branch>` twin |
| CI finishes on a probe branch | `probe-done` | Archives the probe if it still points at the commit CI ran on |
| `gh workflow run branch-hygiene.yml -f dry_run=true` | `preview` | Makes the same decisions as `sweep` with a read-only token and changes nothing |

Every job writes its list to the job summary in the run log.

## Never touched

The workflow never deletes these, by any trigger:

- **The default branch.**
- **Protected branches.** Protection is checked, not assumed: right before each delete, the job reads the branch
  again and asks the rulesets API whether a rule forbids deleting it. If the branch is protected, or a rule
  forbids deletion, or either answer can't be read, the branch is kept.
- **Branches with an open pull request,** as head or as base.
- **The `pre-pr/<head>` twin** of an open pull request's head.
- **`dependabot/*`**, and the `keep` list.
- **A name outside `A-Z a-z 0-9 . _ / -`.**

The sweep also leaves alone anything pushed to or committed to in the last 24 hours. It takes the later of two
dates: the head commit's committer date, and the last push from the repository activity API. If it can't read
either one, it keeps the branch.

## What gets archived or purged

The sweep checks, in this order:

1. **Purge list** (now only `claude/mi-sitemap-seeds`): deleted with **no** tag, because its content must not
   stay in the repo.
2. **CI probes** (the `probes` patterns: `ci/fast-ci-probe-*`, `pre-pr/ci-fast-*probe*`): archived.
3. **`pre-pr/*` and `wip/*` branches whose head commit belongs only to closed pull requests,** merged or
   abandoned: archived.
4. **Any branch with no push and no commit for 7 days:** archived.
5. Anything else is kept.

When a pull request closes, `pr-closed` tags its head if it merged, because the repository auto-deletes merged
branches. It then archives `pre-pr/<branch>` if the twin's head is the pull request's head, or a commit of that
pull request. A twin that holds other work is kept, and the sweep looks at it again later.

`probe-done` archives a probe branch once CI has run on it, if the branch has not been pushed again since.

Pull request and probe cleanups come from an event that says the work is done, so the 24-hour rule doesn't apply
to them. Every other protection does.

## How a branch is archived

The job stops at the first step that fails:

1. Create the annotated tag `archive/<branch>/<YYYY-MM-DD>` (the UTC date) at the branch head. Its message gives
   the last commit, its author and date, and the reason. If the tag already exists, it is reused and never moved.
2. Read the tag back and check that it resolves to the branch head.
3. Read the branch again: it must still point at that commit and must not be protected, and no ruleset may forbid
   deleting it.
4. Delete the branch.

If the tag can't be created or verified, the branch is **not deleted**. The same goes for a branch that moved or
became protected. Each of these is listed under "Not deleted: needs a maintainer" and gets a warning in the log.
A branch that is already gone counts as done. Run the whole thing again and nothing changes: every step is
idempotent.

## The tracking issue

The tracking issue is "Branch hygiene: archived and deleted branches", opened by the first sweep. Each sweep,
the first included, posts its list there:

- **The issue body** is replaced by the latest report. It lists what was archived, each with its tag and restore
  command; what was purged; what was not deleted, and why; and the kept branches, each with its reason.
- **A comment** is added by each sweep that changed anything, so the issue keeps the history.

## Restore a branch

The tracking issue and the job summary give the exact command for each archived branch. From any clone, the
general form is:

```bash
tag="archive/<branch>/<YYYY-MM-DD>"
git fetch origin "refs/tags/$tag:refs/tags/$tag"
git push origin "refs/tags/$tag^{commit}:refs/heads/<branch>"
```

The `^{commit}` matters. The archive tags are annotated, and a branch must point at a commit, not at a tag
object, so `git push origin <tag>:refs/heads/<branch>` is refused.

To list a branch's archive tags, run `git ls-remote --tags origin 'archive/<branch>/*'`. A restored branch is
archived again by the next sweep unless it gets a push, an open pull request, or a `keep` entry.

A purged branch has no tag and can't be restored this way. Its commits may stay reachable for a while, for example
through a pull request's `refs/pull/<n>/head` or GitHub's caches. Removing them completely needs GitHub Support.

## Probe branches

A probe branch tests CI itself, and should live only as long as its run. Prefer one fixed name per probe
(`pre-pr/ci-fast-ci-probe-code`, say) and push to it again for each experiment. If you need a new name, give it
one that matches a `probes` pattern: `probe-done` archives it when its CI run finishes, and the sweep catches any
probe that is more than 24 hours old.

## Keep, purge or add a probe pattern

Edit [`config/branch_keep.yaml`](../../config/branch_keep.yaml) in a pull request. Each entry needs a `name` and a
`reason`:

- `keep` and `probes` names may be globs (`research/*`). A probe pattern that would match every branch is refused.
- `purge` names are exact.

The file is reviewed like code: a change to it changes what the workflow deletes.

## Permissions and safety

- **Token scope.** The workflow's top-level token has no permissions. `contents: write` is granted only to the
  three jobs that tag and delete (`sweep`, `pr-closed`, `probe-done`), each with `pull-requests: read`.
  `preview` reads only. `report` has `issues: write` only.
- **Triggers.** No `pull_request_target`. `pr-closed` runs only for pull requests whose head is in this
  repository. `probe-done` uses `workflow_run`, which runs in the default branch's context. Both check out the
  default branch's script and never run a pull request's or a probe's code.
- **Event values.** Branch names, commit ids and pull request numbers reach the script through `env`, quoted, as
  `--opt="$VAR"`, so a name can't become shell or an option. The script refuses a branch name outside
  `[A-Za-z0-9._/-]`, or one with `..`, `//`, `/.`, or a trailing `.`, `/` or `.lock`.
- **Actions.** Every action is pinned to a full commit SHA, and every checkout drops its credentials.
- **Network.** The API base URL must be HTTPS.
- **Reports.** A report shows only checked branch names. Free text loses backticks, `@` and angle brackets, so
  a report can't mention anyone or break out of its formatting.
- **Branch.** The sweep runs only from `main`, and the issue is updated only for runs on `main`.

## Known limits

- **Workflow files.** GitHub refuses to let `GITHUB_TOKEN` create a ref at a commit that changes
  `.github/workflows/`, unless that change is already on the default branch. A branch like that can't be tagged,
  so it is listed under "Not deleted" and left in place. A maintainer can archive it by hand:
  1. Tag it: `git tag -a "archive/<branch>/<date>" -m "..." origin/<branch>`.
  2. Push the tag.
  3. Check the tag with `git ls-remote`.
  4. Delete the branch.
- **A race of milliseconds.** The REST API has no compare-and-delete for a branch. The job re-reads the head just
  before deleting, so only a push in the moment between that read and the delete could be lost.
- **Tag dates.** A tag carries the date of the run, so a pull request that closes just before midnight UTC may
  get the next day's date.

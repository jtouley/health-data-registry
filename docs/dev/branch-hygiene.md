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
| `gh workflow run branch-hygiene.yml -f dry_run=true` | `preview` | Makes the same decisions as `sweep` with a read-only token and changes nothing, the tracking issue included |

Every job writes its list to the job summary in the run log. A preview's list is there only.

`pr-closed` runs only for pull requests into the default branch.

## Never touched

The workflow never deletes these, by any trigger:

- **The default branch.**
- **Protected branches.** Protection is checked, not assumed: right before each delete, the job reads the branch
  again and asks the rulesets API whether a rule forbids deleting it. If the branch is protected, or a rule
  forbids deletion, or either answer can't be read, the branch is kept.
- **Branches with an open pull request from this repository,** as head or as base. A fork's branch of the same
  name says nothing about ours, so it doesn't count. Open pull requests are checked once when the run plans and
  again right before each delete.
- **A branch that is the base of an open pull request from a fork** is not deleted either. It is listed under
  "Not deleted: needs a maintainer" with the pull request's number, so an outsider's pull request can't keep a
  branch without anyone noticing.
- **The `pre-pr/<head>` twin** of an open pull request's head.
- **`dependabot/*`**, and the `keep` list.
- **A name outside `A-Z a-z 0-9 . _ / -`.**
- **Anything pushed to or committed to in the last 24 hours,** the purge list and a closed pull request's twin
  included. The job takes the later of two dates: the head commit's committer date, and the last push from the
  repository activity API. If it can't read either one, it keeps the branch.

## What gets archived or purged

The sweep checks, in this order:

1. **Purge list** (now only `claude/mi-sitemap-seeds`): deleted with **no** tag, because its content must not
   stay in the repo.
2. **CI probes** (the `probes` patterns: `ci/fast-ci-probe-*`, `pre-pr/ci-fast-*probe*`,
   `pre-pr/ci/fast-mutation-rerun`): archived.
3. **`pre-pr/*` and `wip/*` branches whose head commit belongs only to closed pull requests,** merged or
   abandoned: archived.
4. **Any branch with no push and no commit for 7 days:** archived. This is the owner's decision in issue #67, for
   every namespace, `wip/*` and `claude/*` included; the archive tag keeps the work. CLAUDE.md's branch-cleanup
   rule still says otherwise; [#135](https://github.com/jtouley/health-data-registry/issues/135) settles which applies.
5. Anything else is kept.

A pull request closed without merging is found by its head branch: GitHub's list of the pull requests a commit
belongs to leaves those out for a commit that isn't on the default branch.

**At most 25 branches are deleted per run,** purges first, then archives in name order. The rest are listed under
"Left for the next run", so the first sweep's backlog is spread over a few days and one run's mistakes stay
small. Deletes are paced, two seconds apart, to stay under GitHub's limit on requests that create content. If
GitHub rate-limits the run anyway, it stops deleting and leaves the rest for the next day.

When a pull request closes, `pr-closed` tags its head if it merged, because the repository auto-deletes merged
branches. It then archives `pre-pr/<branch>` if the twin's head is the pull request's head or an earlier commit
of it, and no other open pull request holds that commit. A twin that holds other work is kept, and the sweep
looks at it again later. So is a twin pushed to or committed to in the last 24 hours: the next sweep after that
archives it, because its pull request is closed. In this repo's flow the twin is usually that fresh at merge, so
`pr-closed` mostly just tags the merged head, and the sweep archives the twin a day later.

## How a branch is archived

The job stops at the first step that fails:

1. Create the annotated tag `archive/<branch>/<YYYY-MM-DD>` (the UTC date) at the branch head. Its message gives
   the last commit, its author and date, and the reason. If the tag already exists at that commit, it is reused.
   If it holds another commit (a branch name used again, or a restored branch archived again the same day), the
   tag is `archive/<branch>/<YYYY-MM-DD>-<first 12 characters of the commit id>`. A tag is never moved.
2. Read the tag back and check that it resolves to the branch head.
3. Read the branch again: it must still point at that commit and must not be protected, no ruleset may forbid
   deleting it, and no open pull request may use it. This narrows the race with a push but doesn't close it (see
   [Known limits](#known-limits)).
4. Delete the branch.

If the tag can't be created or verified, the branch is **not deleted**. The same goes for a branch that moved,
became protected or gained an open pull request. Each of these is listed under "Not deleted: needs a maintainer" and gets a warning in the log.
A branch that is already gone counts as done. Run the whole thing again and nothing changes: every step is
idempotent.

## The tracking issue

The tracking issue is "Branch hygiene: archived and deleted branches", opened by the first sweep. The workflow
uses only an open issue with that title that it opened itself (author `github-actions[bot]`), so an issue anyone
else opens with the same title is ignored. Each live sweep, the first included, posts its list there; a preview
never does:

- **The issue body** is replaced by the latest report. It lists what was archived, each with its tag and restore
  command; what was purged; what was not deleted, and why; what was left for the next run; the kept branches,
  each with its reason; and any `keep` entry that matched no branch, which may be a typo.
- **A comment** is added by each sweep that changed anything, so the issue keeps the history.
- **A run that stops early** still writes its report, listing everything it deleted before it stopped. If it
  stopped before writing any report, the workflow adds a comment and leaves the body as it was.

## Restore a branch

The archive tags themselves and the run's job summary are the record to restore from. The tracking issue repeats
the exact command for each archived branch, as a convenience. From any clone, the general form is:

```bash
tag="archive/<branch>/<YYYY-MM-DD>"
git fetch origin "refs/tags/$tag:refs/tags/$tag"
git push origin "refs/tags/$tag^{commit}:refs/heads/<branch>"
```

The `^{commit}` matters. The archive tags are annotated, and a branch must point at a commit, not at a tag
object, so `git push origin <tag>:refs/heads/<branch>` is refused.

To list a branch's archive tags, run `git ls-remote --tags origin 'archive/<branch>/*'`.

Restoring a branch counts as a push, so the sweep keeps it for 24 hours. After that, a restored `pre-pr/*`,
`wip/*` or probe branch whose pull request is closed is archived again by the next sweep, and any other branch is
archived after 7 days with no push or commit, unless it gets an open pull request or a `keep` entry first.

A purged branch has no tag and can't be restored this way. The purge removes only the branch: if an older
archive tag still holds it, the report names that tag so a maintainer can remove it. Its commits may stay reachable for a while, for example
through a pull request's `refs/pull/<n>/head` or GitHub's caches. Removing them completely needs GitHub Support.

## Probe branches

A probe branch tests CI itself, and should live only as long as its run. Prefer one fixed name per probe
(`pre-pr/ci-fast-ci-probe-code`, say) and push to it again for each experiment. If you need a new name, give it
one that matches a `probes` pattern: the daily sweep archives any probe with no push or commit for 24 hours.

## Keep, purge or add a probe pattern

Edit [`config/branch_keep.yaml`](../../config/branch_keep.yaml) in a pull request. Each entry needs a `name` and a
`reason`:

- `keep` and `probes` names may be globs (`research/*`). A probe is archived after 24 hours rather than 7 days, so
  a probe glob must have a fixed part past the first `/`, contain the word `probe`, and match no ordinary name
  such as `main`, `feat/x` or `pre-pr/x`; anything wider is refused. An exact probe name is always allowed.
- A `keep` entry that matches no branch is listed in the report, in case it is a typo.
- `purge` names are exact.

The file is reviewed like code: a change to it changes what the workflow deletes.

## Permissions and safety

- **Token scope.** The workflow's top-level token has no permissions. `contents: write` is granted only to the
  two jobs that tag and delete (`sweep`, `pr-closed`), each with `pull-requests: read`.
  `preview` reads only. `report` has `issues: write` only.
- **Triggers.** Schedule, manual dispatch and `pull_request: closed` only: no `pull_request_target`, no
  `workflow_run`. `pr-closed` runs only for pull requests into the default branch whose head is in this
  repository, and it checks out the default branch's copy of the script. The workflow file itself is the pull
  request's copy on a `pull_request` event, so a pull request that changes `branch-hygiene.yml` can run its own
  steps, with `contents: write`, when it closes. Only people with write access can open such a pull request, but
  review any change to this file as if it could delete branches.
- **Event values.** Branch names, commit ids and pull request numbers reach the script through `env`, quoted, as
  `--opt="$VAR"`, so a name can't become shell or an option. The script refuses a branch name outside
  `[A-Za-z0-9._/-]`, or one with `..`, `//`, `/.`, or a trailing `.`, `/` or `.lock`, and a commit id that is not
  40 hex characters.
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
  before deleting, which narrows the race but doesn't close it: a push in the moment between that read and the
  delete is deleted with the branch, and no archive tag holds it. The 24-hour rule makes this unlikely.
- **Archive tags are not protected yet.** Anyone with write access, or any workflow with `contents: write`, can
  move or delete an `archive/*` tag. A tag ruleset is an owner step, tracked in
  [#133](https://github.com/jtouley/health-data-registry/issues/133).
- **Tag dates.** A tag carries the date of the run, so a pull request that closes just before midnight UTC may
  get the next day's date.

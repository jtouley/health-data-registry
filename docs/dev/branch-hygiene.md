# Branch hygiene: archive tags and inactive branches

[`branch-hygiene.yml`](../../.github/workflows/branch-hygiene.yml) keeps the branch list short without losing
work: every branch it deletes first gets an archive tag that it has checked. It runs in Actions with
`GITHUB_TOKEN`, because a Claude session can push only to `refs/heads` (the session's git proxy refuses tags,
other refs and deletes). The decisions are made by [`scripts/branch_hygiene.py`](../../scripts/branch_hygiene.py),
which is unit-tested against a fake GitHub API (`tests/test_branch_hygiene.py`) and mutation-tested. Issue #67
is the spec.

## What it does

**Daily at 03:41 UTC, live.** It reads every branch and decides:

| Branch | What happens |
|---|---|
| The default branch, a protected branch, the head or base of an open pull request, `dependabot/*`, a `keep` entry in [`config/branch_keep.yaml`](../../config/branch_keep.yaml), or a name outside `A-Z a-z 0-9 . _ / -` | Never touched |
| On the `purge` list in `config/branch_keep.yaml` (now `claude/mi-sitemap-seeds`) | Deleted with **no** tag, because its content must not stay in the repo |
| Last commit more than 7 days ago | Archived |
| A `wip/*` backup whose head commit belongs to a merged pull request (and to no open one) | Archived, whatever its age |
| Anything else | Kept until it has been inactive for 7 days |

"Last commit" is the committer date of the branch head. The exclusions are checked first, so a purge entry
that is protected, kept, or has an open pull request is left alone too.

**Archiving a branch**, in this order, stopping at the first thing that fails:

1. Create the annotated tag `archive/<branch>/<YYYY-MM-DD>` (UTC date of the run) at the branch head. Its message
   names the last commit, its author and date, and the reason. If the tag already exists (a second run the same
   day), it is reused, never moved.
2. Read the tag back and check that it resolves to the same commit as the branch head.
3. Read the branch again and check that it has not moved since the plan.
4. Delete the branch.

A branch whose tag can't be created or verified, or that moved, is **not deleted**: the run lists it under
"Not deleted: needs a maintainer" and prints a warning in the log. A branch already gone counts as done. The
run itself stays green, so the next day's run tries again.

**Every run posts its list** to the tracking issue "Branch hygiene: archived and deleted branches" (opened on
the first run). The issue body is replaced by the latest run's report: what was archived (with each tag and its
restore command), purged, or not deleted, and the kept branches with their reasons. Each run that changed
anything also adds a comment with that list, so the issue keeps the history.

**Merged pull requests.** The repository auto-deletes a pull request's branch on merge. When a pull request from
this repository merges, the `tag-merged` job tags its head commit `archive/<branch>/<date>`, so that branch
keeps an archive tag too. It never deletes anything, skips purge-listed and `dependabot/*` branches, and runs
the default branch's copy of the script, not the pull request's.

## Preview without changing anything

```bash
gh workflow run branch-hygiene.yml -f dry_run=true
```

The `preview` job makes the same decisions with a read-only token and posts "Would archive" and "Would purge" to
the tracking issue. Locally, with a token that can read the repository:

```bash
GH_TOKEN=$(gh auth token) uv run python scripts/branch_hygiene.py run --dry-run \
  --repo jtouley/health-data-registry --report report.md --changes changes.md
```

## Restore a branch

The tracking issue gives the exact command for each archived branch. In general, from any clone:

```bash
tag="archive/<branch>/<YYYY-MM-DD>"
git fetch origin "refs/tags/$tag:refs/tags/$tag"
git push origin "refs/tags/$tag^{commit}:refs/heads/<branch>"
```

The `^{commit}` matters: the archive tags are annotated, and a branch must point at a commit, not at a tag
object, so `git push origin <tag>:refs/heads/<branch>` is refused. List the archive tags of a branch with
`git ls-remote --tags origin 'archive/<branch>/*'`. Restoring a branch with an old last commit means the next
daily run archives it again unless you add a commit, open a pull request, or add it to `keep`.

A purged branch has no tag and can't be restored this way. Its commits may still be reachable for a while (for
example through a pull request's `refs/pull/<n>/head`, or GitHub's caches); removing them completely needs
GitHub Support.

## Keep or purge a branch

Edit [`config/branch_keep.yaml`](../../config/branch_keep.yaml) in a pull request. Each entry needs a `name` and a
`reason`. `keep` names may be globs (`research/*`); `purge` names are exact. The file is control-plane
configuration: a change to it changes what the daily run deletes, so it is reviewed like code.

## Permissions and safety

- The top-level token has no permissions. `sweep` has `contents: write` (tags, deletes) and `pull-requests:
  read`; `preview` has `contents: read` and `pull-requests: read`; `report` has `issues: write` only;
  `tag-merged` has `contents: write` only.
- No `pull_request_target`. `tag-merged` runs only for merged pull requests whose head is in this repository.
  Branch names, commit ids and pull request numbers reach the script through `env`, quoted, as `--opt=value`, so
  a name can't become shell or an option. The script refuses names outside `[A-Za-z0-9._/-]`, `..`, `//`,
  `/.` and a trailing `.`, `/` or `.lock`.
- Every action is pinned to a full commit SHA, and every checkout drops its credentials.
- The API base URL must be HTTPS. The report shows only checked branch names and free text with backticks, `@`
  and angle brackets removed, so it can't mention anyone or break out of its formatting.
- `sweep` runs only from `main`; `report` posts only for runs on `main`.

## Known limits

- **Workflow files.** GitHub refuses to let `GITHUB_TOKEN` create a ref at a commit that changes
  `.github/workflows/` unless that change is already on the default branch. A branch like that can't be tagged,
  so it is reported under "Not deleted" and left in place. A maintainer can archive it by hand:
  `git tag -a "archive/<branch>/<date>" -m "..." origin/<branch>`, push the tag, check it with
  `git ls-remote`, then delete the branch.
- **A race of milliseconds.** The REST API has no compare-and-delete for a branch. The run re-reads the head
  just before deleting, so only a push in the moment between that read and the delete could be lost; on a
  branch idle for 7 days this is very unlikely.
- **Merged-PR tags** use the run's date, so a pull request merged just before midnight UTC may carry the next
  day's date.

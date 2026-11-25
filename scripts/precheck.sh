#!/usr/bin/env bash
# The pre-PR gate, local half: run the CI checks plus a docs-drift check, and stop at the first failure.
# Usage: make precheck [COV_FLOOR=85]
# Then merge origin/main into the branch if it moved, push to pre-pr/<branch> and wait for every check to be
# green (CLAUDE.md, "How changes land").
#
# Not covered here: CodeQL and the Python 3.13 leg of the test matrix, which run only in CI (on the pre-pr push
# and again on the PR). Mutation tests are not part of CI or precheck: they run nightly on main
# (.github/workflows/mutation.yml); `make mutate` runs them locally when you want them. Each step prints one
# PASS/FAIL line; a summary of every step that ran follows at the end.
#
# Portable to macOS's bash 3.2: no mapfile, no associative arrays, and no arrays at all, since expanding an empty
# array under `set -u` is an error before bash 4.4.
set -uo pipefail

UV="${UV:-uv}"
MAKE="${MAKE:-make}"
COV_FLOOR="${COV_FLOOR:-85}"

cd "$(git rev-parse --show-toplevel)" || exit 1

summary=""

finish() {
  echo
  echo "precheck summary:"
  printf '%s' "${summary}"
}

# fail NAME MESSAGE: record and print a FAIL line, then stop the whole run with the next step to take.
fail() {
  summary="${summary}  FAIL  $1
"
  echo "FAIL  $1"
  [ -z "$2" ] || echo "$2"
  finish
  echo
  echo "precheck FAILED at: $1. Fix it, commit, and run make precheck again."
  exit 1
}

# step NAME COMMAND...: run one step, record PASS or FAIL, stop the whole run on FAIL.
step() {
  local name="$1"
  shift
  echo
  echo "==> ${name}"
  local start=$SECONDS
  if "$@"; then
    summary="${summary}  PASS  ${name} ($((SECONDS - start))s)
"
    echo "PASS  ${name}"
  else
    fail "${name} ($((SECONDS - start))s)" ""
  fi
}

tree_state() {
  { git status --porcelain=v1 --untracked-files=all; git diff; } | git hash-object --stdin
}

# make docs and make contracts must change nothing (CI's Checks job runs both). On a clean tree this is `git diff --exit-code`; on a dirty one, it compares the
# tree before and after, so uncommitted work is not mistaken for drift.
docs_drift() {
  local before after
  before="$(tree_state)"
  "${MAKE}" --no-print-directory docs || return 1
  "${MAKE}" --no-print-directory contracts || return 1
  after="$(tree_state)"
  if [ -z "$(git status --porcelain=v1 --untracked-files=all)" ]; then
    git diff --exit-code
    return
  fi
  if [ "${before}" != "${after}" ]; then
    echo "make docs or make contracts changed generated files; commit what they wrote:"
    git status --short
    return 1
  fi
}

if [ -n "$(git status --porcelain=v1 --untracked-files=all)" ]; then
  echo "Note: the working tree has uncommitted changes. CI checks the commit you push, so commit and rerun before"
  echo "pushing to pre-pr/<branch>."
fi

step "pre-commit (all files)" "${UV}" run pre-commit run --all-files --show-diff-on-failure
step "lint" "${MAKE}" --no-print-directory lint
step "typecheck" "${MAKE}" --no-print-directory typecheck
step "test (coverage floor ${COV_FLOOR}%)" "${MAKE}" --no-print-directory test COV_FLOOR="${COV_FLOOR}"
step "smoke" "${MAKE}" --no-print-directory smoke
step "transform (generated models reproduce; dbt build on fixtures)" "${MAKE}" --no-print-directory transform
step "Projects kit" "${MAKE}" --no-print-directory kit
step "generated files drift (make docs and make contracts, then git diff --exit-code)" docs_drift

finish
echo
echo "precheck PASSED. Next: merge origin/main into the branch if it moved (then run precheck again), push this"
echo "commit to pre-pr/<branch>, and wait for every CI check to be green."

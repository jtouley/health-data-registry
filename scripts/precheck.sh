#!/usr/bin/env bash
# The pre-PR gate, local half: run the CI checks plus a docs-drift check, and stop at the first failure.
# Usage: make precheck [BASE=origin/main] [MUTATION_FLOOR=70] [COV_FLOOR=85]
# Then merge origin/main into the branch if it moved, push to pre-pr/<branch> and wait for every check to be
# green (CLAUDE.md, "How changes land").
#
# Not covered here: CodeQL and the Python 3.13 leg of the test matrix, which run only in CI (on the pre-pr push
# and again on the PR). Each step prints one PASS/FAIL line; a summary of every step that ran follows at the end.
#
# Portable to macOS's bash 3.2: no mapfile, no associative arrays, and no arrays at all, since expanding an empty
# array under `set -u` is an error before bash 4.4.
set -uo pipefail

UV="${UV:-uv}"
MAKE="${MAKE:-make}"
BASE="${BASE:-origin/main}"
MUTATION_FLOOR="${MUTATION_FLOOR:-70}"
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

# Mutation reports go in a directory this script creates and removes itself; no caller-supplied path is deleted.
if [ -n "${PRECHECK_REPORTS:-}" ]; then
  fail "settings" "PRECHECK_REPORTS is no longer read: precheck keeps its mutation reports in a temporary directory of its own. Unset it."
fi
REPORTS="$(mktemp -d "${TMPDIR:-/tmp}/precheck-mutation.XXXXXX")" || fail "settings" "Could not create a temporary directory."
trap 'rm -rf "${REPORTS}"' EXIT

tree_state() {
  { git status --porcelain=v1 --untracked-files=all; git diff; } | git hash-object --stdin
}

# make docs must change nothing. On a clean tree this is `git diff --exit-code`; on a dirty one, it compares the
# tree before and after, so uncommitted work is not mistaken for drift.
docs_drift() {
  local before after
  before="$(tree_state)"
  "${MAKE}" --no-print-directory docs || return 1
  after="$(tree_state)"
  if [ -z "$(git status --porcelain=v1 --untracked-files=all)" ]; then
    git diff --exit-code
    return
  fi
  if [ "${before}" != "${after}" ]; then
    echo "make docs changed generated files; commit what it wrote:"
    git status --short
    return 1
  fi
}

if [ -n "$(git status --porcelain=v1 --untracked-files=all)" ]; then
  echo "Note: the working tree has uncommitted changes. CI checks the commit you push, so commit and rerun before"
  echo "pushing to pre-pr/<branch>."
fi

# The mutation steps compare with BASE; check it names a commit before spending time on the other steps.
if ! git rev-parse --verify --quiet "${BASE}^{commit}" > /dev/null; then
  fail "settings: BASE=${BASE}" "BASE is not a commit in this checkout. Run git fetch origin main, or pass BASE=<branch or commit>."
fi

step "pre-commit (all files)" "${UV}" run pre-commit run --all-files --show-diff-on-failure
step "lint" "${MAKE}" --no-print-directory lint
step "typecheck" "${MAKE}" --no-print-directory typecheck
step "test (coverage floor ${COV_FLOOR}%)" "${MAKE}" --no-print-directory test COV_FLOOR="${COV_FLOOR}"
step "smoke" "${MAKE}" --no-print-directory smoke
step "transform (generated models reproduce; dbt build on fixtures)" "${MAKE}" --no-print-directory transform
step "Projects kit" "${MAKE}" --no-print-directory kit
step "docs drift (make docs, then git diff --exit-code)" docs_drift

# Mutation tests as CI shards them: list the changed modules, one step per module, then the floor on the total.
echo
echo "==> mutation: list changed modules since ${BASE}"
if ! modules_json="$("${UV}" run python scripts/mutate_changed.py --list --base "${BASE}")"; then
  fail "mutation: list changed modules since ${BASE}" "scripts/mutate_changed.py --list failed; its message is above."
fi
if ! "${UV}" run python -c 'import json, sys; print("\n".join(json.loads(sys.argv[1])))' "${modules_json}" \
  > "${REPORTS}/modules.txt"; then
  fail "mutation: list changed modules since ${BASE}" "The module list was not JSON: ${modules_json}"
fi
summary="${summary}  PASS  mutation: changed modules since ${BASE}: ${modules_json}
"
echo "PASS  mutation: changed modules since ${BASE}: ${modules_json}"

mkdir "${REPORTS}/shards" || fail "settings" "Could not create ${REPORTS}/shards."
while IFS= read -r module <&3; do
  [ -n "${module}" ] || continue
  step "mutation: ${module}" "${UV}" run python scripts/mutate_changed.py --module "${module}" \
    --report "${REPORTS}/shards/${module}.json"
done 3< "${REPORTS}/modules.txt"
step "mutation floor (${MUTATION_FLOOR}% of all mutants)" "${UV}" run python scripts/mutate_changed.py \
  --aggregate "${REPORTS}/shards" --expect "${modules_json}" --floor "${MUTATION_FLOOR}"

finish
echo
echo "precheck PASSED. Next: merge origin/main into the branch if it moved (then run precheck again), push this"
echo "commit to pre-pr/<branch>, and wait for every CI check to be green."

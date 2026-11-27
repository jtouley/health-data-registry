#!/usr/bin/env bash
# The pre-PR gate, local half: run everything CI runs, in CI's order, and stop at the first failure.
# Usage: make precheck [BASE=origin/main] [MUTATION_FLOOR=70] [COV_FLOOR=85]
# Then push to pre-pr/<branch> and wait for every check to be green (CLAUDE.md, "How changes land").
#
# Not covered here: CodeQL and the Python 3.13 leg of the test matrix run only in CI, so the pre-pr push still
# has to go green. Each step prints one PASS/FAIL line; a summary of every step that ran follows at the end.
set -uo pipefail

UV="${UV:-uv}"
MAKE="${MAKE:-make}"
BASE="${BASE:-origin/main}"
MUTATION_FLOOR="${MUTATION_FLOOR:-70}"
COV_FLOOR="${COV_FLOOR:-85}"
REPORTS="${PRECHECK_REPORTS:-mutation-report}"

cd "$(git rev-parse --show-toplevel)" || exit 1

summary=()

finish() {
  echo
  echo "precheck summary:"
  printf '  %s\n' "${summary[@]}"
}

# step NAME COMMAND...: run one step, record PASS or FAIL, stop the whole run on FAIL.
step() {
  local name="$1"
  shift
  echo
  echo "==> ${name}"
  local start=$SECONDS
  if "$@"; then
    summary+=("PASS  ${name} ($((SECONDS - start))s)")
    echo "PASS  ${name}"
  else
    summary+=("FAIL  ${name} ($((SECONDS - start))s)")
    echo "FAIL  ${name}"
    finish
    echo
    echo "precheck FAILED at: ${name}. Fix it, commit, and run make precheck again."
    exit 1
  fi
}

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

smoke() {
  if "${MAKE}" -n smoke > /dev/null 2>&1; then
    "${MAKE}" --no-print-directory smoke
  else
    echo "No smoke target in the Makefile; skipped."
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
step "smoke" smoke
step "Projects kit" "${MAKE}" --no-print-directory kit
step "docs drift (make docs, then git diff --exit-code)" docs_drift

# Mutation tests as CI shards them: list the changed modules, one step per module, then the floor on the total.
modules_json="$("${UV}" run python scripts/mutate_changed.py --list --base "${BASE}")" || {
  summary+=("FAIL  mutation: list changed modules since ${BASE}")
  finish
  exit 1
}
summary+=("PASS  mutation: changed modules since ${BASE}: ${modules_json}")
mapfile -t modules < <(python3 -c 'import json, sys; print("\n".join(json.loads(sys.argv[1])))' "${modules_json}")
rm -rf "${REPORTS}"
mkdir -p "${REPORTS}"
for module in "${modules[@]}"; do
  [ -n "${module}" ] || continue
  step "mutation: ${module}" "${UV}" run python scripts/mutate_changed.py --module "${module}" \
    --report "${REPORTS}/${module}.json"
done
step "mutation floor (${MUTATION_FLOOR}% of all mutants)" "${UV}" run python scripts/mutate_changed.py \
  --aggregate "${REPORTS}" --expect "${modules_json}" --floor "${MUTATION_FLOOR}"

finish
echo
echo "precheck PASSED. Next: push this commit to pre-pr/<branch> and wait for every CI check to be green."

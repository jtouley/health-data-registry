# Changelog fragments

Each pull request that users would notice adds one small file here instead of editing `CHANGELOG.md`. Separate
files never conflict, so merging one pull request no longer makes every other open one conflict
([#268](https://github.com/jtouley/health-data-registry/issues/268)). Never edit `CHANGELOG.md` directly: at release
time `scripts/changelog.py assemble` folds the fragments into it and deletes them.

## Write a fragment

1. Name the file after your branch or pull request, with `/` turned into `-`: branch `source/cdc_lyme_county`
   becomes `changelog.d/source-cdc_lyme_county.md`. Letters, digits, `.`, `_` and `-` only.
2. Give it one or more of these headings, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) terms:
   `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security`.
3. Under each heading, one or more Markdown bullets saying what a clinician or contributor will notice. Nested
   bullets and wrapped lines are fine. Nothing else goes in the file: no other headings, no text before the first
   heading.

```markdown
### Added

- CDC Lyme disease cases by county, 2001–2023 (`cdc_lyme_county`), with its draft contract.
```

To change an entry later in the same pull request, edit your fragment.

## What CI checks

The Checks job runs `python3 scripts/changelog.py check --base origin/main` on every pull request and pre-pr push
(`make precheck` runs it too). It fails when:

- the change touches `src/`, `catalog/`, `contracts/` or `semantic/` and adds or changes no fragment;
- the change edits `CHANGELOG.md` without deleting fragments (only the release assembly does that);
- any fragment here is malformed, with the file, line and what to fix.

Main, the merge queue and nightly runs skip it.

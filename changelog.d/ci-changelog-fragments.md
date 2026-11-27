### Changed

- Contributors add a `changelog.d/` fragment per pull request instead of editing `CHANGELOG.md`, which
  `scripts/changelog.py assemble` writes at release time. CI fails a pull request that touches `src/`,
  `catalog/`, `contracts/` or `semantic/` without one, so merges no longer make every other open pull request
  conflict.

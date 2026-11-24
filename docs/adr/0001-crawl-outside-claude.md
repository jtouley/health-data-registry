# 0001. The crawl runs on a schedule, outside the chat

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "Hosting and storage"; "Architecture"; README tab, "Sandbox problems seen" and proposed ADR 001

## Context

Fetching public health data from inside a chat failed in practice. Network allowlists blocked the Detroit ArcGIS
Hub and CDC's C. auris JSON. Client-rendered pages gave the fetcher an empty shell. Secondary sources crept in: the
first C. auris draft used the news, and a re-pull from NNDSS changed the finding. Some sources are PDF-only.
A chat is the wrong place to discover, parse and check a source.

## Decision

Fetching and snapshotting happen outside the chat: a scheduled job (a weekly GitHub Action) or a person running
`health-data-registry refresh <dataset>|--all`. Each run validates against the dataset's contract and writes a new
snapshot. A nightly check flags any source that no longer matches its contract, and a shape change opens a PR.

At question time the server reads only from snapshots. Sources whose terms forbid mirroring
([ADR 0009](0009-licences-decide-mirroring.md)) are read live from the catalogued publisher host for each question,
through the same contract-checked path, held in memory only and never stored or published (amended 2026-11-24:
the earlier one-time `refresh` onto the user's machine is gone). The server never browses, searches or scrapes during a chat.

## Consequences

- Answers are fast and repeatable: the same snapshot gives the same number.
- A source that breaks fails in CI, not in front of a fellow.
- Data can be up to a week old; every result shows its retrieval date so this is visible.
- Fetches of restricted sources depend on the publisher being up; when a dataset has no snapshot, the tool says
  so in plain words.

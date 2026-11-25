# Data products and their ports

For developers and maintainers. Three ODPS data products describe what this project offers, one file each in
`contracts/`. `health-data-registry validate` checks all of them on every run; `product.py` holds the rules.

| Product | File | Ports |
|---|---|---|
| Open health data hub | `health-data-registry.odps.yaml` | One port per catalogued source; its contract is the source's own |
| ID conference abstracts | `id-conference-abstracts.odps.yaml` | `id_abstracts_index` (public), `id_abstracts_text` (private tier) |
| Michigan HIV data | `michigan-hiv.odps.yaml` | One port per source, grouped and never merged |

Only the hub's ports are published by `export-public`. The other products' ports each have their own ODCS
contract in `contracts/ports/` and read catalogued sources through `inputContracts`.

## The official schemas

`validate` checks every `*.odps.yaml` against Bitol's ODPS v1.1.0 JSON schema, and every `*.odcs.yaml` against
the ODCS v3.2.0 schema. Both are vendored byte for byte in `src/health_data_registry/schemas/`. Their source URLs
and sha256 are recorded in `product.py` (`ODPS`, `ODCS`), and a copy whose hash differs is refused. Nothing
fetches a schema at run time. To restore a copy, or to move to a new version after changing the recorded URL and
hash on purpose, run `uv run python scripts/vendor_schemas.py`. The pre-commit whitespace hooks skip that folder,
so the bytes stay as published.

## What every port declares

Each output port's `customProperties` carries:

- `tier`: `public` or `private`.
- `hosting`: `public-hub`, `private-hf` (a private Hugging Face dataset only), `live` (read from the publisher per
  question, never stored) or `none` (not built yet).
- `releaseTag`: `release-<run date>` for a hosted port.
- `sla`: `refresh` (the same as each catalogued source's `refresh`), `release` and `support`.
- `feedback`: the [contract-finding issue form](../../.github/ISSUE_TEMPLATE/contract-finding.yml).
- `licences`: exactly the licence ids of the sources it reads. They are inherited, never chosen.

A port's answers also carry its sources' licence terms and caveats (`inherited_context`).

## Public and private

- A public port goes on the public hub only when every source is catalogued, `hosting: public`, and licensed for
  a public copy (`registry.REDISTRIBUTABLE_LICENCES`).
- A public port never reads a private-tier port. Its contract has no column marked `tier: private`.
- A private-tier port is never on the public hub. Abstract text under a restrictive licence (CROI, ESCMID,
  CC BY-NC-ND supplements) lives only in private Hugging Face datasets.

## Stubs

A port whose sources aren't catalogued yet is a stub: `stub: true`, `hosting: none`, a draft contract, and
`pendingInputs` naming each source, its licence (or `unresolved: ...`) and the pull request or task that fills it.
Anything that would change a number goes in `openQuestions`, never in the contract as a guess.

| Port | Tier | Fills from |
|---|---|---|
| `id_abstracts_index` | public, unhosted until its licence question is answered | #29 (IDWeek, SHEA, APIC, ICID), #30 (CROI) |
| `id_abstracts_text` | private | #30 (CROI text), #29 (private-tier rows) |
| `mi_mdhhs_hiv_sti` | public | tasks 78–80 |
| `mi_detroit_ryan_white` | public; programme data, not surveillance | tasks 78–80 |
| `mi_detroit_ehe` | public; programme data, not surveillance | tasks 78–80 |
| `mi_ahead_wayne` | public | tasks 78–80 and the AHEAD download |
| `mi_aidsvu` | private | tasks 78–80 |
| `mi_detroit_dhd_sti_annual` | private (no terms of reuse stated) | #19 (`detroit_dhd_epi_annual`) |

In the Michigan product (`combine: grouped-never-merged`), each port reads exactly one source and declares its
own `countedUnit` and `dataKind`. Detroit and Out-Wayne stay separate series wherever the publisher splits them.

## Port contracts

Each file in `contracts/ports/` is an ODCS v3.2.0 contract that the registry's contract parser also accepts. In
addition it carries:

- **Fingerprints.** `customProperties.fingerprints` is the history, one `{version, interface, docs}` per version.
  The interface fingerprint covers the shape (tables, column names, types, required, keys). The docs fingerprint
  covers the meaning (descriptions, rules, properties; not open questions). Changing the shape needs a new major
  version and changing the meaning needs a new minor. Bump the version, then run
  `uv run python scripts/fingerprint_ports.py`.
- **Quality rules.** Every rule has a `name`, a `dimension` (completeness, conformity, …), a `severity` (`error`
  or `warn`) and a `dbtTest` custom property, so the silver stage can generate it as a dbt test. A port is served
  only when every error-severity rule ran and passed (`product.servable`); a rule with no result fails closed.
- **Conformed keys.** `conformedKey` on a column names the silver dimension it joins (`dim_concept`, `dim_place`,
  `dim_time`, built on `feat/contract-silver`). Condition ids are MeSH, organisms NCBI Taxonomy, places FIPS or
  ISO3. A tag proposed by dictionary matching is stored as `proposed` with its own review-state column, and is
  never a fact until a maintainer accepts it (hard rules 1 and 9).

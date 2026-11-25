# Data products and their ports

For developers and maintainers. Three ODPS data products describe what this project offers, one file each in
`contracts/`. `health-data-registry validate` checks all of them on every run; `product.py` holds the rules.

| Product | File | Ports |
|---|---|---|
| Open health data hub | `health-data-registry.odps.yaml` | One port per catalogued source; its contract is the source's own |
| ID conference abstracts | `id-conference-abstracts.odps.yaml` | `id_abstracts_index` (private until its licence question is answered), `id_abstracts_text` (private tier) |
| Michigan HIV data | `michigan-hiv.odps.yaml` | One port per source, grouped and never merged |

Only the hub's ports are published by `export-public`. The other products' ports each have their own ODCS
contract in `contracts/ports/` and read catalogued sources through `inputContracts`.

## The official schemas

`validate` checks every `*.odps.yaml` against Bitol's ODPS v1.1.0 JSON schema, and every `*.odcs.yaml` against
the ODCS v3.2.0 schema. Both are vendored byte for byte in `src/health_data_registry/schemas/`. Their source URLs
and sha256 are recorded in `product.py` (`ODPS`, `ODCS`), and a copy whose hash differs is refused. Nothing
fetches a schema at run time. To restore a copy, or to move to a new version after changing the recorded URL and
hash on purpose, run `uv run python scripts/vendor_schemas.py`. The pre-commit whitespace hooks skip that folder,
and `.gitattributes` marks it `-text` so git never rewrites its line endings; the bytes stay as published. A
missing or altered schema is one `FAIL` line from `validate`, never a crash.

## What every port declares

Each output port's `customProperties` carries:

- `tier`: `public` or `private`.
- `hosting`: `public-hub`, `private-hf` (a private Hugging Face dataset only), `live` (read from the publisher per
  question, never stored) or `none` (not built yet).
- `releaseTag`: `release-<run date>` for a hosted port.
- `sla`: `refresh`, `release` and `support`. A port refreshes no more often than its slowest catalogued source
  (daily, weekly, monthly, quarterly, annual, irregular), so a port reading an annual and an irregular source
  says `irregular`.
- `feedback`: the [contract-finding issue form](../../.github/ISSUE_TEMPLATE/contract-finding.yml).
- `licences`: exactly the licence ids of the sources it reads. They are inherited, never chosen.

A port's answers also carry its sources' licence terms and caveats (`inherited_context`).

## Public and private

- A public port, whatever its hosting (`none` and `live` included), reads only sources licensed for a public
  copy (`registry.REDISTRIBUTABLE_LICENCES`). A restricted licence (CROI, ESCMID, AIDSVu, WHO, no terms stated),
  an `unresolved: ...` licence or a source read live only fails `validate`: make the port private tier.
- A public port goes on the public hub only when, in addition, every source is catalogued with `hosting: public`
  in its catalog entry (the entry's `hosted` is true).
- A public port never reads a private-tier port. Every column of a port contract declares `tier: public` or
  `tier: private`; a column with no tier counts as private, and a public port has none.
- A private-tier port is never on the public hub. It is stored in a private Hugging Face dataset
  (`hosting: private-hf`) only when every source may be stored: a catalogued source kept in its own reviewed
  private store (`private_store: true`, registry tier `private`), or one whose licence a maintainer has reviewed
  as allowing a stored copy (`product.PRIVATE_COPY_LICENCES`). A source read live only (registry tier `live`:
  WHO, the Detroit and MDHHS dashboards) never is, so such a port is `live` or `none`. Abstract text under a restrictive licence (CROI, ESCMID, CC BY-NC-ND
  supplements) is never public.
- `hosted` on a hub port is `true` or `false`; any other value fails `validate`, and only `true` counts as hosted.
- If two hub ports claim one source, `validate` fails, `get_provenance` refuses that one source in plain words,
  and `export-public` leaves that source's product unnamed (`data_product_problem`); every other source works.

## Stubs

A port whose sources aren't catalogued yet is a stub: `stub: true`, `hosting: none`, a draft contract, and
`pendingInputs` naming each source, its licence (or `unresolved: ...`) and the pull request or task that fills it.
Anything that would change a number goes in `openQuestions`, never in the contract as a guess.

| Port | Tier | Fills from |
|---|---|---|
| `id_abstracts_index` | private until a maintainer records that its sources' metadata may be published; then public | #29 (IDWeek, SHEA, APIC, ICID), #30 (CROI) |
| `id_abstracts_text` | private | #30 (CROI text), #29 (private-tier rows) |
| `mi_mdhhs_hiv_sti` | private until MDHHS's terms are read | tasks 78–80 |
| `mi_detroit_ryan_white` | private until its terms are read; programme data, not surveillance | tasks 78–80 |
| `mi_detroit_ehe` | private until its terms are read; programme data, not surveillance | tasks 78–80 |
| `mi_ahead_wayne` | private until AHEAD's terms are read | tasks 78–80 and the AHEAD download |
| `mi_aidsvu` | private | tasks 78–80 |
| `mi_detroit_dhd_sti_annual` | private (no terms of reuse stated) | `detroit_dhd_epi_annual` (catalogued in #19); its port columns still to declare |

In the Michigan product (`combine: grouped-never-merged`), each port reads exactly one source and declares its
own `countedUnit` and `dataKind` (`surveillance`, `programme` or `unresolved: ...`), the same `dataKind` as its
port contract. Detroit and Out-Wayne stay separate series wherever the publisher splits them.

A port contract whose publisher's suppression rule hasn't been read says `unreliableRule: {kind: unresolved}`,
never `kind: none` (which means the publisher states no rule). That keeps the contract draft and the port an
unserved stub, and the rule is listed in `openQuestions` (hard rules 1 and 4). Only port contracts may say
`unresolved`; a catalogued source's contract never may.

## Port contracts

Each file in `contracts/ports/` is an ODCS v3.2.0 contract that the registry's contract parser also accepts. In
addition it carries:

- **Fingerprints.** `customProperties.fingerprints` is the history, one `{version, interface, docs}` per version.
  The interface fingerprint covers the shape (tables, column names, types, required, keys). The docs fingerprint
  covers the meaning (descriptions, rules, properties; not open questions). Changing the shape needs a new major
  version and changing the meaning needs a new minor. Bump the version, then run
  `uv run python scripts/fingerprint_ports.py`. The script edits only the fingerprints item and checks that the
  rest of the file reads back unchanged before it writes.
- **Quality rules.** Every rule has a `name`, a `dimension` (completeness, conformity, uniqueness, …), a
  `severity` (`error` or `warn`) and a `dbtTest` custom property, so the silver stage can generate it as a dbt
  test. Each contract has at least one error-severity rule, including a uniqueness rule on its grain
  (`abstract_key`; place, period and indicator). Optional key columns carry a `warn` fill-rate rule that catches a
  column left empty on every row; the real fill rate to expect is an open question until rows are seen.
- **Serving.** Each rule's outcome is `passed`, `failed` (the data broke the rule: a contract finding), `error`
  (the rule couldn't run, e.g. a renamed column: a code fix) or `not_run`; `product.rule_report` keeps them apart.
  A port is served only when every error-severity rule `passed` (`product.servable`); anything else fails closed.
- **Conformed keys.** `conformedKey` on a column names the silver dimension it joins (`dim_concept`, `dim_place`,
  `dim_time`, built on `feat/contract-silver`). Condition ids are MeSH, organisms NCBI Taxonomy, places FIPS or
  ISO3. A tag proposed by dictionary matching is stored as `proposed` with a review-state column, and is never a
  fact until a maintainer accepts it (hard rules 1 and 9). In `id_abstracts_index` one state covers all of a
  row's condition and organism tags together, and another its place tags; a bridge table with one state per tag
  is a follow-up.

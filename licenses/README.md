# Data licences and terms

This repository holds code only. **No data is committed here.** Each dataset's licence, terms and citation are
recorded in its catalog entry (`catalog/`) and returned with every result.

What the project may do with a source depends on its terms:

| Source class | Examples | Terms | Snapshot to the Hugging Face dataset? | How the server reads it |
|---|---|---|---|---|
| US federal | CDC (NNDSS, WONDER, AtlasPlus), NCHS, Census, CMS | Public domain (17 U.S.C. § 105), unless a dataset says otherwise | Yes | From the snapshot; the agency is cited with every result |
| WHO | Global Health Observatory | WHO Terms and Conditions for datasets ([terms](https://www.who.int/about/policies/publishing/data-policy/terms-and-conditions)): public health purposes, attribution, no commercial promotion, only minimal changes | **No, never mirrored** | Fetched from WHO at run time; the terms are shown with every result |
| IHME Global Burden of Disease | GBD estimates | IHME's own free-of-charge non-commercial user agreement | **No, never fetched** | Not read at all; use the IHME GBD connector for GBD numbers |
| State and county reports | Health department surveillance reports, dashboards, PDFs | Varies by state and county; often unstated | Only when the publisher's terms clearly allow it | Fetched at run time; cite the report, its publisher and its year |

Rules:

- A source is snapshotted only when its terms allow redistribution. When in doubt, it is fetched at run time.
- Attribution and non-commercial terms are passed to the user, never dropped.
- Publishers' suppression flags and uncertainty ranges are kept as published.
- If a publisher's terms change, the catalog entry is updated in the same pull request as any change to how the
  source is stored.

Questions about a source's terms: open an issue.

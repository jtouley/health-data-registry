# Security policy

## Reporting a vulnerability

Please don't describe a vulnerability in a public issue, pull request or discussion.

Report it privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**
(<https://github.com/jtouley/health-data-registry/security/advisories/new>). Include what is affected, the steps to
reproduce it and the impact you expect.

If the **Report a vulnerability** button is missing, open a public issue titled "Security contact request", with no
details, and the maintainer will set up a private channel.

You will get a first response within 14 days. The report and the fix are discussed in the private advisory, and the
advisory is published, with credit if you want it, once a fixed release is out.

## What to report

- **Fetcher SSRF or host escape:** a source adapter that can be made to request a host outside the catalogued
  publisher hosts, a non-HTTPS URL, a private or link-local address, or to follow a redirect to one.
- **SQL sandbox escapes:** a `query_sql` or `query_metric` call that reads or writes files, attaches a database,
  loads an extension, reaches the network, changes settings, or reads a table outside the catalogued read-only views.
- **Workflow and token issues:** a GitHub Actions workflow that runs untrusted input, has more permissions than it
  needs, or exposes a secret or the OIDC token; anything that could tamper with a release, its signatures, its SBOM
  or its provenance, or with the published Hugging Face snapshots.
- **Wrong numbers presented as correct:** a result returned without its caveats, uncertainty range, suppression
  flag or licence, or a metric that silently sums rates or totals. A wrong number that looks authoritative is a
  safety issue for clinicians and researchers, so report it here if it is systematic or could be triggered on
  purpose. A one-off wrong value can go in a public **Wrong number** issue instead.
- **Privacy:** any path by which the server accepts, stores or logs patient data, or joins record-level public-use
  files in a way that could re-identify a person.

## Out of scope

- The publishers' own sites and APIs (CDC, WHO, Census, state and county portals). Report those to the publisher.
- Disagreements about a publisher's published figures. Open a **Wrong number** issue if we reproduce them wrongly.

## Supported versions

Only the latest release and `main` are supported. Fixes land on `main` and ship in the next tagged release.

## Verifying a release

Each GitHub release carries Sigstore signatures (`*.sigstore.json`), SLSA provenance (`*.intoto.jsonl`) and a
CycloneDX SBOM. PyPI uploads use trusted publishing, so no long-lived upload token exists.

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
- **SQL sandbox escapes:** a `query_sql` call (or any tool) that reads or writes files, attaches a database,
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

## How changes are reviewed

This is a one-person project. There is no second maintainer to approve pull requests, so `main` requires no
approving review, and GitHub doesn't let an author approve their own pull request. OpenSSF Scorecard's
Code-Review and Branch-Protection checks therefore stay low. That score reflects the project honestly, and we
don't work around it with a second account or a bot approval.

What stands in for a human reviewer:

- **A required gate a pull request can't change.** The Trusted gate (`.github/workflows/trusted-gate.yml`) runs
  `main`'s own copy of the ratchet (`src/health_data_registry/ratchet.py`), never the pull request's: through a
  ruleset "required workflows" rule pinned to `refs/heads/main` where the plan allows it, and otherwise on
  `pull_request_target` with a read-only token, reading the pull request only as data. It fails a change that
  lowers a coverage or mutation floor, adds a skip, pragma, type-ignore, noqa or lint relaxation that isn't in the
  allowlist, drops tests, removes or loosens a required check, touches the sealed evaluation sets or bypasses the git
  hooks. [docs/dev/README.md](docs/dev/README.md#the-trusted-gate) lists the rules and what they can't catch.
- **A recorded review round on every pull request.** An independent `/code-review` (a fresh agent with no context
  beyond the pull request number) posts its findings on the pull request, and so does each matching review skill,
  then one fix round addresses or files every finding before merge. Unresolved review threads block the merge.
  ([CLAUDE.md](CLAUDE.md), "How changes land".)
- **Only commits that already passed CI reach a pull request branch** (a branch ruleset requiring the CI result),
  and the git hooks run on every commit.

## Supported versions

Only the latest release and `main` are supported. Fixes land on `main` and ship in the next tagged release.

## Verifying a release

Each GitHub release carries Sigstore signatures (`*.sigstore.json`), SLSA provenance (`*.intoto.jsonl`) and a
CycloneDX SBOM. PyPI uploads use trusted publishing, so no long-lived upload token exists.

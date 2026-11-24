# 0011. Local stdio server first; hosted Hugging Face Space later

- **Status:** Proposed (distribution is an open question in the spec)
- **Date:** 2026-11-24
- **Spec:** "Hosting and storage"; "Milestones" (phase 4, "Open up: hosted connector"); "Open questions for Jason"
  (Distribution); README tab, "Open roadmap"

## Context

A local server needs no hosting, costs nothing and sends nothing anywhere but the publishers. A hosted server is
one URL to add as a claude.ai custom connector, which suits fellows who don't use a terminal, but it needs rate
limits, a no-logging policy and security review, and free Spaces sleep when idle.

## Decision

Ship first as a local MCP server over stdio, installed with
`uvx --from git+https://github.com/jtouley/health-data-registry health-data-registry serve`, and
`uvx health-data-registry serve` once on PyPI. Add a hosted server later: a Hugging Face Space (Docker, FastMCP over
Streamable HTTP, free CPU), read-only, rate-limited, with no logs of questions. Google Cloud Run is the fallback if
the Space's cold starts hurt.

## Consequences

- Until then, users need Claude Desktop or Claude Code, plus uv and Git; [docs/setup.md](../setup.md) and the
  `walk_me_through_install` helper take a clinician through the one-time install ([ADR 0012](0012-built-for-clinicians.md)).
- claude.ai and ChatGPT custom connectors wait for the hosted server.
- The server code must not assume stdio, so the same tools can run over Streamable HTTP.

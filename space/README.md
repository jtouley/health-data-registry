---
title: health-data-registry
emoji: 🩺
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
license: apache-2.0
short_description: Read-only public infectious disease data for Claude
pinned: false
---

# health-data-registry (hosted connector)

A read-only [MCP](https://modelcontextprotocol.io) server that answers clinician-researchers' questions about
infectious disease with public, population-level data from CDC, WHO and other publishers, each number with its
source, version and method.

**To use it:** in Claude, add a custom connector with this Space's address followed by `/mcp`, for example
`https://<owner>-<space>.hf.space/mcp`. There is no sign-in.

- It reads only published, tagged releases of the
  [open data hub](https://huggingface.co/datasets/jtouley/health-data-registry), and reads WHO data live from
  WHO without storing it.
- It never asks for, accepts or stores patient information.
- Your questions are not logged. See [PRIVACY.md](PRIVACY.md).

Source code, contracts and documentation: <https://github.com/jtouley/health-data-registry>.

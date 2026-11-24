# health-data-registry

Ask Claude about public infectious-disease data from CDC, WHO and other publishers. Every answer names its source,
version and method. The tool only reads public data and never asks for patient information.

This npm package is a small starter: it runs the health-data-registry MCP server (a Python program, published on
PyPI) through [uv](https://docs.astral.sh/uv/), Astral's free Python tool runner. It has no dependencies and runs
nothing when it is installed.

Setup for clinicians: <https://github.com/jtouley/health-data-registry/blob/main/docs/setup.md>

## Claude Desktop

Add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then quit Claude Desktop fully and
open it again:

```json
{
  "mcpServers": {
    "health-data-registry": {
      "command": "npx",
      "args": ["-y", "health-data-registry"]
    }
  }
}
```

This needs Node.js on the computer. If uv isn't installed, the tool stops with plain-words steps; add
`"--install-uv"` after `"health-data-registry"` in `args` to let it install uv from astral.sh on first start.

## Claude Code

```bash
claude mcp add health-data-registry -- npx -y health-data-registry
```

## What it runs

```text
uvx --no-config --default-index https://pypi.org/simple --python 3.12 \
    --from health-data-registry==<this package's version> health-data-registry serve
```

`npx -y health-data-registry --print-command` shows the exact command. Environment variables such as `HDR_HUB`,
`HDR_LAKE` and `HDR_CACHE` pass through unchanged.

Licence: Apache-2.0.

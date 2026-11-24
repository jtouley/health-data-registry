# Distribution: PyPI, npm, Claude Desktop extension, MCP Registry

How a clinician gets the server, and how each channel is built. Researched 24 Nov 2026; sources at the end.

## The channels and how they relate

PyPI is the only place the server's code is published. Every other channel is a pointer to one exact PyPI release.

| Channel | What it is | What the clinician needs | Built from |
|---|---|---|---|
| PyPI `health-data-registry` | The Python package (`health-data-registry serve`) | uv | `release.yml` |
| npm `health-data-registry` | A dependency-free Node launcher: finds uv, runs `uvx --from health-data-registry==X.Y.Z health-data-registry serve` | Node.js and uv (or `--install-uv`) | `npm/`, `npm-publish.yml` |
| `health-data-registry-X.Y.Z.mcpb` | Claude Desktop extension: the same launcher, run by Claude Desktop's built-in Node.js | Claude Desktop; uv, or tick "Install uv if missing" | `packaging/mcpb/`, `npm-publish.yml` (attached to the GitHub release) |
| MCP Registry `io.github.jtouley/health-data-registry` | Metadata only: points to the PyPI and npm packages | nothing; clients and directories read it | `packaging/server.json`, published by hand |
| Hosted URL (custom connector) | The server on the web | nothing: paste a URL in Settings → Connectors | not built yet |

Versions move together: `pyproject.toml`, `npm/package.json`, `packaging/server.json` (top level and each
package) and `packaging/mcpb/manifest.json`. `npm test` and the `test` job of `npm-publish.yml` fail when they
drift. Bump all four in the same PR as the CHANGELOG section.

### The launcher (`npm/lib/launcher.js`)

- Looks for `uv` on `PATH`, then in `$UV_INSTALL_DIR`, `$XDG_BIN_HOME`, `~/.local/bin` (the official installer's
  default), `~/.cargo/bin`, Homebrew (`/opt/homebrew/bin`, `/usr/local/bin`) and, on Windows, the WinGet links
  folder. Claude Desktop starts servers with a short `PATH`, so the fixed folders matter.
- If uv is missing it prints plain-words steps to stderr and exits 1. Only with `--install-uv` or
  `HDR_INSTALL_UV=1` does it run Astral's installer: `https://astral.sh/uv/install.sh` (redirects are followed
  only within `astral.sh`) or `irm https://astral.sh/uv/install.ps1 | iex` on Windows.
- Runs `uvx --no-config --default-index https://pypi.org/simple --python 3.12 --from health-data-registry==X.Y.Z
  health-data-registry serve` (or `uv tool run ...` when only `uv` is found), with stdio inherited and the
  environment passed through, except uv settings that would change the package index or Python mirror
  (`UV_INDEX*`, `UV_EXTRA_INDEX_URL`, `UV_FIND_LINKS`, `UV_PYTHON_INSTALL_MIRROR`). `--no-config` ignores
  `uv.toml` for the same reason. Arguments after the launcher's own flags replace `serve` (for example
  `npx -y health-data-registry refresh --all`).
- Writes nothing to stdout itself: stdout is the MCP channel. Nothing runs at `npm install`.
- What uv itself downloads: the package and its dependencies from PyPI, and, when no Python 3.12 is on the
  computer, a managed Python build from Astral's python-build-standalone releases.

Run its tests with `cd npm && npm test`.

### The Claude Desktop extension (`packaging/mcpb/manifest.json`)

An `.mcpb` is a zip with a `manifest.json` that Claude Desktop installs on double-click. Ours is a `node` type
bundle: Claude Desktop ships its own Node.js, so the clinician installs no runtime for the launcher. Its settings
screen offers "Install uv if missing" (off by default; it sets `HDR_INSTALL_UV`) and a data folder (`HDR_LAKE`,
default `~/health-data`). The `npm-publish.yml` `mcpb` job stages the manifest with `npm/bin`, `npm/lib` and
`npm/package.json` under `server/` and packs it with `@anthropic-ai/mcpb@2.1.2`. To build one locally:

```bash
mkdir -p build/mcpb/server
cp packaging/mcpb/manifest.json LICENSE NOTICE build/mcpb/
cp -R npm/bin npm/lib npm/package.json build/mcpb/server/
npx --yes @anthropic-ai/mcpb@2.1.2 pack build/mcpb health-data-registry.mcpb
```

Why not the `uv` bundle type: manifest 0.4 adds `server.type: "uv"`, where the host installs Python and the
dependencies from a bundled `pyproject.toml`. It is marked experimental and Claude's own MCPB page still
recommends Node.js, so we use the Node launcher and revisit once `uv` bundles are documented as supported in
Claude Desktop. The Claude directory no longer accepts `.mcpb` submissions, so the file is distributed from the
GitHub release.

### The MCP Registry (`packaging/server.json`)

The registry holds metadata, not code. It checks ownership of each package it lists:

- npm: `mcpName` in `npm/package.json` must equal the `name` in `server.json` (`io.github.jtouley/health-data-registry`).
- PyPI: the package README (the PyPI description) must contain `mcp-name: io.github.jtouley/health-data-registry`,
  which may sit in an HTML comment. **The root README.md does not have it yet**; add
  `<!-- mcp-name: io.github.jtouley/health-data-registry -->` before the first PyPI release that is listed.
- The `io.github.jtouley/` namespace is proved by logging in with GitHub as `jtouley`.

`description` is at most 100 characters (schema `2025-12-11`).

## Owner steps

Do these in order. Nothing here is automated yet.

1. **PyPI first.** Configure the PyPI trusted publisher for `release.yml` (environment `pypi`), add the
   `mcp-name` comment to README.md, then tag `vX.Y.Z`. The npm job waits up to 40 minutes for that version on
   PyPI and fails rather than ship a launcher that cannot start.
2. **npm account.** Create an npm account for the GitHub owner (or an npm org) with two-factor authentication on.
   The unscoped name `health-data-registry` and the scope `@jtouley/health-data-registry` were both free on
   24 Nov 2026 (`https://registry.npmjs.org/<name>` returned 404); we use the unscoped name so the command is
   `npx -y health-data-registry`.
3. **First npm publish by hand.** npm can only attach a trusted publisher to a package that already exists. From
   a clean checkout of the release tag: `cp LICENSE NOTICE npm/ && cd npm && npm publish --access public`.
4. **Trusted publisher on npm.** npmjs.com → package → Settings → Trusted publishing → GitHub Actions:
   organisation or user `jtouley`, repository `health-data-registry`, workflow `npm-publish.yml`, environment
   `npm`. The new configuration must see a successful publish within 2 days or it expires. Then, under
   Publishing access, require two-factor authentication and disallow tokens. Create the `npm` environment in
   the GitHub repository settings (tags `v*` only, with a required reviewer if wanted).
5. **Later releases** go through `npm-publish.yml`: trusted publishing needs npm 11.5.1+ (Node 24 has it) and
   `id-token: write`, which only the `npm` job has. `--provenance` is explicit; npm adds it by default for
   trusted publishing from a public repository.
6. **MCP Registry.** After both packages are live: install `mcp-publisher` from the
   [registry releases](https://github.com/modelcontextprotocol/registry/releases), then from `packaging/`:
   `mcp-publisher login github` and `mcp-publisher publish`. Bump `server.json` with every release.
7. **Hosted URL.** When the hosted server exists, add a `remotes` entry (`streamable-http`, its URL) to
   `server.json` and make the custom connector the README's main path.

## Sources

- Claude: [Build a desktop extension with MCPB](https://claude.com/docs/connectors/building/mcpb.md),
  [Getting started with local MCP servers on Claude Desktop](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop),
  [Custom connectors using remote MCP](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp)
- MCPB: [MANIFEST.md](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md),
  [hello-world-uv example](https://github.com/modelcontextprotocol/mcpb/tree/main/examples/hello-world-uv)
- MCP Registry: [package types and verification](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx),
  [server.json reference](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/generic-server-json.md),
  [schema 2025-12-11](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json)
- npm: [Trusted publishing](https://docs.npmjs.com/trusted-publishers), [npm trust](https://docs.npmjs.com/cli/v11/commands/npm-trust)
- uv: [installation](https://docs.astral.sh/uv/getting-started/installation/)

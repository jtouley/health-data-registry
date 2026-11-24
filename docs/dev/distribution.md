# Distribution

How the server's workflows reach clinicians outside a checkout.

## The plugin and the Claude Projects kit

Both are generated from the workflow records in `src/health_data_registry/helpers.py` by
`src/health_data_registry/distribution.py`; edit the words in `helpers.py` only.

- **Plugin** (`plugin/`, kept in git, written by `make docs`). `plugin/.claude-plugin/plugin.json` takes its version
  from `pyproject.toml`; `plugin/skills/<name>/SKILL.md` holds one skill per workflow, with only the Agent Skills
  frontmatter fields (`name`, `description`) so it loads in chat; `plugin/.mcp.json` lists one `http` server, this
  one, at `HDR_PUBLIC_URL` under `[tool.health-data-registry]` in `pyproject.toml`. That URL is a `.invalid`
  placeholder until the hosted server has a domain; change it there and run `make docs`. The IHME GBD and PubMed
  connectors are the fellow's own and are not in the plugin.
- **Marketplace** (`.claude-plugin/marketplace.json` at the repo root). A marketplace is the directory that holds
  that file, and claude.ai adds one by repository URL (**Customize > Plugins > Add > Add marketplace**), so it has
  to sit at the root. Its one entry points at `./plugin`.
- **Projects kit** (`dist/projects-kit/`, never committed; `make kit` builds it, CI uploads it as the
  `projects-kit` artifact and each release attaches it as a zip). It holds `project-instructions.md` (to paste into
  the Project's instructions; checked against a 16,000-character limit), `knowledge/*.md` (data conventions, the
  publication-outlook method, the rubric, the case-report standard and one card per catalog entry: Markdown only,
  never data files) and `skills/<name>.zip` (one upload per skill, the skill folder at the top level, with fixed
  timestamps so the bytes are the same on every build).

`tests/test_distribution.py` fails when a generated file drifts, a manifest has a field Anthropic's docs don't
list, a zip's layout or bytes change, or the instructions grow past the limit. Formats follow
[Plugin structure and testing](https://claude.com/docs/plugins/build.md), the
[marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference.md) and
[Create custom skills](https://claude.com/docs/skills/how-to.md#package-your-skill). The 16,000-character limit is
documented for the [new version of Projects](https://code.claude.com/docs/en/claude-projects.md); no limit is
published for classic chat Projects, so it is applied to both until one is.

# Plugins and skills

This repo ships the EduPage MCP server **and** one agent skill
(`school-day-summary`). This document explains every artifact in the package,
how to install it in each host, and how to keep the two copies of
`SKILL.md` from drifting.

## What is in the box

| Path | What it is |
| --- | --- |
| `src/edupage_mcp/skills/school-day-summary/SKILL.md` | **Canonical** skill. Ships inside the Python wheel (`tool.setuptools.package-data`), so the manual install copies it out of `site-packages`. This is the copy you edit. |
| `plugin/` | The **Agent Plugins 1.0** package. One directory that also satisfies the Claude Code plugin layout, so it installs from both ecosystems without a third copy of the skill. |
| `plugin/plugin.json` | Agent Plugins 1.0 manifest. Closed schema (`$schema`, `name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, `extensions`). |
| `plugin/mcp.json` | Agent Plugins 1.0 MCP server entry: `edupage`, launched as `uvx edupage-mcp-full` over stdio. |
| `plugin/skills/` | **Generated mirror** of the canonical tree. Written only by `scripts/sync_skills.py`; never hand-edited. |
| `plugin/.claude-plugin/plugin.json` | Claude Code manifest for the same directory. Adds the parts Claude supports and Agent Plugins does not: `mcpServers` and a `userConfig` block. |
| `.claude-plugin/marketplace.json` | The marketplace catalog. **Read by both** Claude Code and GitHub Copilot from this one file. |

### Why SKILL.md exists twice, and not three times

Agent Plugins 1.0 §6.1 fixes component locations: a skill must be at
`<plugin root>/skills/<name>/SKILL.md`, and the manifest cannot point anywhere
else. The canonical copy lives in the Python package, so a plugin directory
needs a second copy. That is the floor — two copies.

The third copy would be a Claude-only `skills/` tree. It is avoided by making
`plugin/` satisfy **both** layouts at once:

* Agent Plugins reads `plugin/plugin.json` + `plugin/mcp.json` + `plugin/skills/`.
* Claude Code reads `plugin/.claude-plugin/plugin.json` + the default `plugin/skills/`.

Both clients therefore load the same `plugin/skills/school-day-summary/SKILL.md`.
The extra `.claude-plugin/` directory inside `plugin/` is inert to Agent Plugins:
component discovery only looks at `skills/` (non-recursive) and `mcp.json`, so an
unrelated directory is simply not discovered.

> **Note on the Claude manifest location.** Claude resolves the manifest at
> `<plugin root>/.claude-plugin/plugin.json`. Because the marketplace entry's
> source is `./plugin`, the plugin root *is* `plugin/`, so the Claude manifest
> belongs at `plugin/.claude-plugin/plugin.json` — not at the repo root. The
> repo root holds only the marketplace file. `tests/unit/test_plugin_sync.py`
> asserts this so the two cannot be swapped by accident.

### On the Claude `command` plugin source

Claude Code supports
`"source": {"source": "command", "command": "..."}`, where a locally installed
command prints the plugin directory's absolute path. That would make the mirror
unnecessary, and it is the better long-term shape. It is **not** used here for a
concrete reason: the command runs on the *installing user's* machine from their
home directory, before any of this repository has been fetched, so it must be an
executable that already exists on that machine. This package ships exactly one
console script — `edupage-mcp-full`, which starts the MCP server — and a new
entry point would have to be added to `pyproject.toml` and `src/edupage_mcp/`.
So the marketplace uses a relative-path source instead. When an
`edupage-plugin-dir` (or similar) entry point exists, swapping the entry to the
`command` form is a two-line change.

Relative-path sources also avoid the `mode: link` problem: link mode is
**refused on Windows**, and this repo is developed on Windows.

## Install

### (a) GitHub Copilot CLI / VS Code / the Copilot app — via marketplace

```bash
copilot plugin marketplace add oliverhruby/edupage-mcp
copilot plugin install edupage@edupage-plugins
```

Copilot finds the marketplace at `.claude-plugin/marketplace.json` — GitHub's
docs list `.github/plugin/marketplace.json` **or** `.claude-plugin/marketplace.json`,
so one file serves both ecosystems. No `.github/plugin/` copy is needed.

In the Copilot app: **Customize → Plugins → add marketplace → `oliverhruby/edupage-mcp`**.
In Copilot cloud agent, add the marketplace to `extraKnownMarketplaces` in the
repository's `.github/copilot/settings.json`.

### (b) GitHub Copilot CLI — direct from git, no marketplace

```bash
copilot plugin install oliverhruby/edupage-mcp:plugin
```

Or locally, during development:

```bash
copilot plugin install ./plugin
```

### (c) Claude Code — via marketplace

```bash
claude plugin marketplace add oliverhruby/edupage-mcp
claude plugin install edupage@edupage-plugins
```

Validate a checkout before publishing either way:

```bash
claude plugin validate ./plugin --strict   # plugin manifest
claude plugin validate . --strict          # marketplace manifest
```

### (d) Hosts with no plugin system (opencode, Cursor, Windsurf)

These read Agent Skills but not plugins. Install just the skill from git:

```bash
npx skills add oliverhruby/edupage-mcp/tree/main/src/edupage_mcp/skills
```

The `src/edupage_mcp/skills` path is the **canonical** tree, so this route needs
no sync step. Configure the MCP server separately in the host's own MCP config —
`edupage-mcp-full` is on PyPI, so the portable entry is:

```json
{
  "mcpServers": {
    "edupage": {
      "command": "uvx",
      "args": ["edupage-mcp-full"],
      "env": {
        "EDUPAGE_USERNAME": "your-username",
        "EDUPAGE_PASSWORD": "your-password",
        "EDUPAGE_SUBDOMAINS": "yourschool"
      }
    }
  }
}
```

`uvx` is required — the plugin does not vendor a virtualenv, and
`plugin/mcp.json` deliberately never names a local path.

## Credentials

The server reads three environment variables at startup:

| Variable | Required | Meaning |
| --- | --- | --- |
| `EDUPAGE_USERNAME` | yes | EduPage login (a **parent** account for child data). |
| `EDUPAGE_PASSWORD` | yes | EduPage password. |
| `EDUPAGE_SUBDOMAINS` | no | Comma-separated school subdomains. When set it is a strict allowlist; leave it empty to auto-discover. |

You can also authenticate interactively with the `login` tool, which takes
`username`/`password` parameters and falls back to these variables.

**Why the plugin does not ship them.** Agent Plugins 1.0 §9.2 states that a
plugin "MUST NOT embed credentials or other secrets in `env`", and §9.1 notes
the client "MAY inherit, omit, or sanitize ambient variables" — there is no
portable credential-reference field and no environment-variable interpolation
anywhere in the format. An `env` block with empty or placeholder values would
be worse than useless: §9.1 says configured entries *overlay and replace*
same-name ambient entries, so it would overwrite credentials you had already
exported. `plugin/mcp.json` therefore declares no `env` at all, and the three
variables are read from your environment.

Claude Code is the exception, because it *does* have a portable credential
surface: `plugin/.claude-plugin/plugin.json` declares a `userConfig` block, so
Claude prompts for the values on enable, stores the password in the platform's
secure credential store (`sensitive: true`), and substitutes them into the
server's `env` as `${user_config.*}`.

## Keeping the mirror in sync

`src/edupage_mcp/skills/` is canonical. `plugin/skills/` is generated. After
editing any skill:

```bash
python scripts/sync_skills.py            # copy (default; --write is the same)
python scripts/sync_skills.py --check    # verify only; exit 1 on drift
```

`--check` compares the trees byte-for-byte in both directions and names every
offending path, so a stale mirror fails CI instead of shipping:

```
DRIFT: plugin/skills does not match src/edupage_mcp/skills (1 missing/changed, 0 stale). Run: python scripts/sync_skills.py
  missing/changed: plugin/skills/school-day-summary/SKILL.md
```

The same rules are covered by `tests/unit/test_plugin_sync.py`, which runs
`--check` against the committed tree and asserts the manifests stay inside their
published schemas.

## Checking whether a new host supports this

1. **Agent Skills?** Look for `SKILL.md` with `name` + `description`
   frontmatter at `<dir>/<name>/SKILL.md`, where `name` equals the directory
   name. This is the floor — if a host reads this, route (d) works.
2. **Agent Plugins 1.0?** Look for `plugin.json` at the plugin root with
   `$schema` `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`, plus
   `skills/` and `mcp.json`. That combination means this repo installs as-is.
3. **Copilot-compatible marketplaces?** A host whose CLI accepts
   `plugin marketplace add owner/repo` and reads
   `.github/plugin/marketplace.json` **or** `.claude-plugin/marketplace.json`
   can install via route (a).
4. **Claude `command` sources?** If the host runs a local command to materialize
   a plugin directory, an entry point that prints that path removes the need for
   the mirror entirely (see above).
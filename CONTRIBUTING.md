# Contributing

Thanks for contributing to `edupage-mcp`.

## Local setup

```bash
git clone https://github.com/oliverhruby/edupage-mcp.git
cd edupage-mcp
uv sync
```

Alternative setup:

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e .
```

Quick checks:

```bash
python -m py_compile src/edupage_mcp/__init__.py
python -m edupage_mcp
```

## Architecture and implementation

### High-level design

```
MCP client (opencode / Claude / Cursor ...)
        |  stdio JSON-RPC
        v
edupage-mcp-full (FastMCP server, mcp<2, console entry point edupage-mcp-full)
        |  thin facade
        v
edupage-api (community library, endpoint/login/parsing logic)
        v
EduPage web services (HTTPS)
```

This project is intentionally a thin wrapper around `edupage-api`.

### Key files

- `src/edupage_mcp/__init__.py`: entire MCP server (all tools + `main()`).
- `src/edupage_mcp/__main__.py`: `python -m edupage_mcp` entry.
- `pyproject.toml`: package metadata + console script.
- `requirements.txt`: editable/dev install support.

### Session and state management

Core runtime state:

```python
_clients = {}          # subdomain -> Edupage
_two_factor = {}       # subdomain -> TwoFactorLogin (pending 2FA)
_active_subdomain = None
```

Tools resolve clients through `_require_client(subdomain)`.

### 2FA flow

- `two_factor_check_confirmed`: checks whether device confirmation is approved.
- `two_factor_finish`: completes 2FA via device or explicit code.

### Serialization and errors

- `_serialize()` converts dataclasses/enums/datetime-rich objects to plain JSON.
- `_run()` catches `edupage_api` exceptions and returns MCP-friendly error payloads.

### Dependency isolation

`mcp<2` is intentionally pinned. `uvx` and editable installs run in isolated
environments to avoid global package conflicts.

## Release process

Version source of truth is `pyproject.toml`.

- Tag format: `vX.Y.Z`
- PyPI publish: `.github/workflows/publish.yml` (OIDC trusted publishing)
- GitHub release notes: `.github/workflows/release.yml` (auto-generated)
- GHCR image publish: `.github/workflows/publish-container.yml`

Ensure tag version matches `pyproject.toml` version.

## CI quality gates

`main` branch requires these checks:

- `quality-gates / python-sanity`
- `quality-gates / docker-mcp-smoke`
- `security / pip-audit`
- `container-security / trivy-image`
- `upstream-coverage / coverage-drift`

## Upstream coverage drift check

To keep parity with `edupage-api`, CI runs
`.github/workflows/upstream-coverage.yml`.

It verifies each public `Edupage` method is either:

- covered by wrapper usage in `src/edupage_mcp/__init__.py`, or
- explicitly listed in `scripts/edupage_api_ignored_methods.json` with a reason.

Run locally:

```bash
python scripts/check_edupage_api_coverage.py
```

## Conventional Commits

All commit messages should follow the Conventional Commits specification
(<https://conventionalcommits.org/>).  The format is:

```
<type>(<scope>): <short description>
```

**Types**

- `feat` – new feature (e.g. a new tool, a new API endpoint)
- `fix` – bug fix or regression
- `docs` – documentation only
- `refactor` – code change that neither adds nor fixes a bug
- `perf` – performance improvement
- `test` – adding or fixing tests
- `chore` – routine maintenance (bump version, config)
- `style` – formatting, missing semi‑colons, etc.
- `build` – CI/CD changes, dependency updates
- `revert` – revert a previous commit

**Example messages**

```
feat(timetable_range): add get_timetable_range wrapper
fix: typo in README upgrade section
docs: update README with upgrade instructions
refactor: move _parse_date helper to shared module
```

If a change is breaking, add a footer:

```
BREAKING CHANGE: the `get_timetable_range` function now requires a `subdomain` argument.
```

**Why we use it**

- The GitHub Actions workflow that generates release notes splits commits into
  *Added*, *Changes* and *Upgrade* based on the commit type.
- It also makes auto‑generated `CHANGELOG.md` files possible.

**How to add a commit**

1.  Choose the appropriate `<type>`.
2.  Optionally add a `<scope>` that describes the area affected
    (e.g. `timetable`, `timetable_range`, `meals`, `messages`, `mcp`, `pyproject`,
    `readme`).
3.  Write a short, imperative description (present tense, no period).
4.  Add a body (optional) for motivation or details.
5.  Add a footer (optional) for breaking changes or co‑authors.
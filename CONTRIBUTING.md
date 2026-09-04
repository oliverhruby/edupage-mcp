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

```text
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

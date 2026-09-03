# AGENTS.md — edupage-mcp-full

Guidance for AI agents (and humans) working on this repository. This file is
about **maintaining the code** — it is *not* end-user runtime documentation
(that lives in [README.md](README.md)).

## What this project is

A stdio Model Context Protocol (MCP) server that exposes the Python
[`edupage-api`](https://github.com/EdupageAPI/edupage-api) library as MCP tools.
Published to PyPI as **`edupage-mcp-full`**; the GitHub repo is the canonical
source.

**Architecture rule:** this repo is deliberately a **thin wrapper** around
`edupage-api`. All EduPage endpoint/parsing/login complexity belongs upstream,
not here. When a feature breaks, check `edupage-api` first before reimplementing
logic. Do not grow a scraping layer here.

## Layout

| Path | Purpose |
|---|---|
| `src/edupage_mcp/__init__.py` | **The whole server**: all tools + `main()`. |
| `src/edupage_mcp/__main__.py` | `python -m edupage_mcp` entry. |
| `pyproject.toml` | Packaging; console script `edupage-mcp-full = "edupage_mcp:main"`. |
| `README.md` | End-user docs (install, usage, tools). |
| `requirements.txt` | Dev install (`-e .`). |

## Conventions (keep these consistent)

- **One file.** All tools live in `__init__.py`. Keep it that way unless it
  becomes unmanageable.
- **Every tool** is a function decorated with `@_tool` and defined as:
  ```python
  @_tool
  def my_tool(arg: str = None, subdomain: str = None) -> dict:
      """Description. Note if it mutates EduPage state (Writes: X)."""
      def go():
          client = _require_client(subdomain)
          ...
          return {...}
      return _run(go, "my_tool")
  ```
  - `_tool` registers the fn with FastMCP when MCP is installed, else keeps it
    callable for tests.
  - `_run` wraps exceptions → returns `{"isError": True, ...}` (JSON-RPC result).
  - Sub-tools that need parsing helpers should reuse `_serialize`, `_parse_date`,
    `_resolve_target`, `_find_student` rather than reimplementing.
- **Read-only vs write.** `get_*` tools read only. Tools that send messages,
  order meals, or switch accounts write — say so in the docstring, and mark in
  the README tool table.
- **Multi-school state.** Sessions are keyed by subdomain:
  ```python
  _clients = {}          # subdomain -> Edupage
  _two_factor = {}       # subdomain -> TwoFactorLogin
  _active_subdomain = None
  _roles = {}            # subdomain -> "student" | "parent" | "teacher"
  ```
  Every data tool takes an optional `subdomain` and resolves through
  `_require_client(subdomain)` (falls back to `_active_subdomain`). Role-aware
  tools use `_roles[sub]` to determine account type and behave accordingly
  (e.g. parent → switch to student account; student → direct timetable).
- **Student cache.** `_get_students_cached(client, subdomain)` caches visible
  students keyed by `(subdomain, role)` for `_STUDENT_CACHE_TTL` (5 min) to avoid
  redundant API calls across tools. Cache is cleared on `clear_student_cache`,
  auto-login, and re-login. Parent accounts use `get_all_students()`; student/
  teacher use `get_students()`.
- **Tiered name matching.** `_find_student(client, name, subdomain)` matches by
  tier (highest confidence first): full name → first name → last name → short
  name (`name_short`, e.g. `'Novák V.'`). `_find_student_all` returns all
  candidates with their tier/confidence; ambiguous multi-matches surface all
  candidates to the caller rather than silently picking one.
- **Auto-login / auto-discovery.** At startup `main()` auto-logs-in when
  `EDUPAGE_USERNAME`+`EDUPAGE_PASSWORD` are set. If `EDUPAGE_SUBDOMAINS` is set it
  logs into each subdomain (`_autologin`); otherwise it auto-discovers a single
  school via the portal (`_autodiscover` → `client.login_auto`). Failures and
  schools needing 2FA are recorded in `_autologin_failures` and surfaced by
  `get_schools`.
- **JSON output.** Return plain JSON serialisable via `_serialize` (handles
  dataclasses, enums, `datetime`). Don't return raw `edupage-api` objects.

## Git commit policy

- Use **Conventional Commits** for every commit message.
- Format: `<type>(<scope>): <description>` (scope optional when not useful).
- Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
  `build`, `ci`, `chore`, `revert`.
- Keep the subject short and imperative (`add`, `fix`, `update`), with no
  trailing period.
- Before pushing, check recent history (`git log --oneline -10`) and rewrite
  non-conforming local commit subjects to conventional format.

## Dependency pinning

`pyproject.toml` pins `mcp<2`. Reason: `mcp 2.x` renamed `FastMCP` → `MCPServer`
and changed the API. We target the FastMCP v1 API. Keep `mcp<2`. Bump
`edupage-api` as needed (it can rise freely).

> On shipping versions as of v0.1.0: `mcp` resolves to **1.29.1** (latest 1.x,
> no known CVEs) and `edupage-api` to **0.12.5** (latest). Staying on `mcp<2`
> is a deliberate **API-compat** decision, not a security pin. Revisit the v2
> `MCPServer` port only if v1.x stops receiving security fixes or you need v2
> features — it is a real code change, not a version bump.

## Security scanning

- **Dependabot** (repo-level): security alerts + automated security updates for
  known CVEs are on by default for public repos and are enabled here.
  `.github/dependabot.yml` adds **weekly version-update PRs** for `pip` and
  `github-actions`. Do not remove the `ignore: mcp >=2.0.0` block (matches the
  intentional pin).
- **`pip-audit`** (`.github/workflows/security.yml`): scans the whole installed
  dependency tree — direct + transitive — against OSV on every push/PR to main
  and weekly. A run that finds a CVE **fails the workflow**; fix the pinned
  version in `pyproject.toml` and re-verify with `pip-audit` locally before
  releasing.
- **Quality gates** (`.github/workflows/quality-gates.yml`):
  - `python-sanity` compiles `src/edupage_mcp/__init__.py` and verifies
    `pip install .` from source.
  - `docker-mcp-smoke` builds the Docker image and performs an MCP stdio
    handshake (`initialize` + `tools/list`) against the container.
- **Trivy container scan** (`.github/workflows/container-security.yml`): builds
  the Docker image and scans for vulnerabilities on every push/PR to main and
  weekly. The job fails on `HIGH`/`CRITICAL` findings (`ignore-unfixed: true`).
  Use `.trivyignore` only for temporary, documented exceptions.
- **Upstream coverage drift** (`.github/workflows/upstream-coverage.yml`):
  checks that public `edupage-api` `Edupage` methods are covered by wrapper calls
  or explicitly ignored in `scripts/edupage_api_ignored_methods.json` with a
  reason. Also runs a scheduled canary against the latest `edupage-api`.

`main` branch protection requires these checks:

- `quality-gates / python-sanity`
- `quality-gates / docker-mcp-smoke`
- `security / pip-audit`
- `container-security / trivy-image`
- `upstream-coverage / coverage-drift`

Local check:

```bash
pip install pip-audit && pip-audit   # run inside the project venv
```

To run any workflow manually from `gh`:

```bash
gh workflow run security.yml --repo oliverhruby/edupage-mcp
gh workflow run quality-gates.yml --repo oliverhruby/edupage-mcp
gh workflow run container-security.yml --repo oliverhruby/edupage-mcp
gh workflow run upstream-coverage.yml --repo oliverhruby/edupage-mcp
```

## Build / verify

```bash
# from repo root — install once
uv sync            # or: python -m venv .venv && .venv/bin/python -m pip install -e .

# sanity: compile + list tools over a real MCP handshake
python -m py_compile src/edupage_mcp/__init__.py
python -m edupage_mcp     # then drive an MCP client; tools/list should show all
```

There is no test suite; a manual MCP `tools/list` after any addition is the
verification step. After adding/renaming a tool, update the README "Tool
reference" table and the tool count in the "What it provides" blurb.

## Publishing (PyPI)

Publishing uses **OIDC trusted publishing** via GitHub Actions — no API token.
See comments at the top of `.github/workflows/publish.yml` for the one-time PyPI
registration (project `edupage-mcp-full`, workflow name `publish.yml`).

To release a new version:

1. Bump `version` in `pyproject.toml`.
2. Commit + push.
3. Push a tag matching the version, e.g. `git tag v0.1.0 && git push --tags`.
4. The `publish` workflow builds and uploads automatically (uses the `release`
   GitHub environment, if configured).

> If the `release` environment has a "required reviewers" gate, approve the run
> in the GitHub Actions UI. Local rebuilds (`python -m build` + `twine upload`)
> still work as a non-OIDC fallback.

Keep the README accurate (install, tools, counts).

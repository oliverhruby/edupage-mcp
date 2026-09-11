# AGENTS.md

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
  auto-login, and re-login. Parent accounts get their linked children by parsing
  the school homepage (`_get_parent_children`); a failed homepage fetch raises (it
  is never treated as "no children"), and there is no roster fallback (a parent's
  school-wide roster is not the same as their linked children). Student/teacher
  use `get_students()`.
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
gh workflow run e2e-ci.yml --repo oliverhruby/edupage-mcp   # live e2e (needs secrets)
```

## Build / verify

```bash
# from repo root — install once
uv sync            # or: python -m venv .venv && .venv/bin/python -m pip install -e .

# sanity: compile + list tools over a real MCP handshake
python -m py_compile src/edupage_mcp/__init__.py
python -m edupage_mcp     # then drive an MCP client; tools/list should show all
```

There is a small **offline** unit suite in `tests/unit/` for deterministic
wrapper logic that the live suite can't pin down (parent→child timetable path,
`get_day_summary` discovery-first dispatch, `EDUPAGE_SUBDOMAINS` scoping):

```bash
python -m pytest tests/unit -q
```

A manual MCP `tools/list` after any addition is still the verification step.
There **is** also a live integration suite in `tests/e2e/` that logs into the
real schools with the helper's own EduPage account and exercises every read-only
tool:

```bash
powershell -ExecutionPolicy Bypass -File run_e2e.ps1     # local runner
```

- `run_e2e.ps1` sets `EDUPAGE_E2E=1` and strips `cvcmalacky` from
  `EDUPAGE_SUBDOMAINS` (only `zssturovamalacky` and `iprskola` are approved).
- Requires `EDUPAGE_USERNAME`/`EDUPAGE_PASSWORD` of the helper's account; run
  only on the owner's machine.
- Read-only by design: conftest monkeypatches `edupage-api` write methods to
  raise, and `test_manifest.py` partitions every tool so no write-capable tool
  is ever invoked.
- Known upstream defects are tracked as `known_error` substrings and **fail the
  suite if the message changes** (e.g. `find_student` no-such-student,
  `get_grades` `percent` UnboundLocalError in edupage-api 0.12.5). Fix them in
  our wrapper or bump upstream when they drift.
- A drift report is written to `reports/e2e-report.json` (gitignored) with full
  payload previews — that is the **local** report and stays on the machine.

### CI run (option 3 — zero-data, public-safe)

`.github/workflows/e2e-ci.yml` runs the same suite **on manual
`workflow_dispatch` only** (never on push/PR). It is currently **dormant**:
GitHub-hosted runners cannot reach `edupage.org` (HTTP 000 / `Errno 101 Network
is unreachable`, confirmed 2026-09-10), so the live suite needs a **self-hosted
runner** with home-ISP egress — until one is registered, use the **local
`run_e2e.ps1`** path below as the verification path. Privacy is enforced by
design, not by log masking:

- Credentials come only from `EDUPAGE_USERNAME` / `EDUPAGE_PASSWORD` **secrets**
  (injected via env; masked in logs). No credentials are in the repo.
- The workflow sets `EDUPAGE_E2E_CI=1` → the suite switches to **zero-data
  output**: report entries and assertion messages carry no payload previews, no
  child ids, no identity text (`in_ci()` in `conftest.py`), so no child data
  can appear in public job logs even on failure.
- Child-identity drift is checked against the **one-way sha256 fingerprints** in
  `tests/e2e/expected_children.fingerprint.json` (`fingerprint.py`): raw names/
  ids are never committed or printed; a mismatch fails the suite showing only
  the two hashes.
- No artifacts are uploaded (artifacts in a public repo are downloadable by
  anyone) and the report is not published.
- The write-guard + manifest partition still apply, so the CI run is read-only.

**Regenerating fingerprints** (owner only, never on CI): the exact check needs
the gitignored `tests/e2e/.local.e2e.json`. When EduPage changes or children
change legitimately and the fingerprint test fails locally:

1. Confirm the parsed children locally (run the suite with `.local.e2e.json`
   present to see the full identity diff).
2. Regenerate with the live generator and commit the new
   `expected_children.fingerprint.json` — the commit message must not contain
   child identities.

### Privacy policy (summary)

This repo and its CI are public. Exact child identities (names + person ids)
must never be committed, printed in CI logs, or uploaded as artifacts. Local
exact-identity checks read the gitignored `tests/e2e/.local.e2e.json`; the only
CI-committed evidence is the one-way fingerprint file and a zero-data report.

After adding/renaming a tool, update the README "Tool reference" table, the
tool count in the "What it provides" blurb, and the `tests/e2e/test_manifest.py`
partition (and `test_readonly_tools.py` manifest if it should be polled).

## Publishing (PyPI)

Publishing uses **OIDC trusted publishing** via GitHub Actions — no API token.
See comments at the top of `.github/workflows/publish.yml` for the one-time PyPI
registration (project `edupage-mcp-full`, workflow name `publish.yml`).
GitHub Releases are created automatically on tag push by
`.github/workflows/release.yml` using GitHub's generated release notes.
Container images are published to GHCR on tag push by
`.github/workflows/publish-container.yml`.

To release a new version:

1. Bump `version` in `pyproject.toml`.
2. Commit + push.
3. Push a tag matching the version, e.g. `git tag v0.1.0 && git push origin v0.1.0`.
4. The `publish` workflow builds and uploads automatically (uses the `release`
   GitHub environment, if configured).

> **Tags are created manually.** `.github/workflows/auto-tag.yml` was removed:
> tags pushed by `github-actions[bot]` via `GITHUB_TOKEN` do **not** re-trigger
> the tag-push workflows (Release / Publish / Publish Container), so an "auto
> tag" left the tag un-published. Push the exact tag ref yourself
> (`git push origin v0.1.0` — avoid `--tags`, which pushes every local tag,
> including stale ones).

> If the `release` environment has a "required reviewers" gate, approve the run
> in the GitHub Actions UI. Local rebuilds (`python -m build` + `twine upload`)
> still work as a non-OIDC fallback.

Keep the README accurate (install, tools, counts).

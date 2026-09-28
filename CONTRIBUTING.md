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

A single `two_factor_finish` tool resolves a pending 2FA login: with a `code`
(emailed/app) it completes directly; without one it polls the
device-confirmation flag for up to `poll_seconds` (default 60) and returns a
pending status that can be retried.

### Serialization and errors

- `_serialize()` converts dataclasses/enums/datetime-rich objects to plain JSON.
- `_run()` catches `edupage_api` exceptions and returns MCP-friendly error payloads.

### Dependency isolation

`mcp<2` is intentionally pinned. `uvx` and editable installs run in isolated
environments to avoid global package conflicts.

## Release process

Version source of truth is `pyproject.toml`.

- Tag format: `vX.Y.Z`
- PyPI publish: [.github/workflows/publish.yml](.github/workflows/publish.yml) (OIDC trusted publishing)
- GitHub release notes: [.github/workflows/release.yml](.github/workflows/release.yml) (auto-generated)
- GHCR image publish: [.github/workflows/publish-container.yml](.github/workflows/publish-container.yml)
- MCP Registry publish: [.github/workflows/publish-mcp-registry.yml](.github/workflows/publish-mcp-registry.yml) (OIDC login + mcp-publisher)

Ensure tag version matches `pyproject.toml` version.

## CI/CD workflows

All automation lives in `.github/workflows/` (10 workflows). This reference
lists every workflow — trigger, job, and purpose. `main` **branch protection**
requires the jobs marked **required** to pass before a push is allowed.

### Release pipelines (tag push `v*`)

Pushing a version tag (`vX.Y.Z`, e.g. `git push origin v0.5.0`) runs the four
release workflows in parallel. Version source of truth is `pyproject.toml`;
tag-triggered workflows first run `scripts/check_tag_matches_version.py` and
fail if the tag does not match the package version.

- **[Publish](.github/workflows/publish.yml)** – publishes
  `edupage-mcp-full` to PyPI via OIDC trusted publishing (no API token; the
  one-time publisher registration is documented in the file header). A final
  step smoke-checks the Glama directory listing for `oliverhruby/edupage-mcp`
  through the Glama API: it needs the `GLAMA_API_KEY` secret, skips cleanly when
  that secret is unset, and on a non-200 only emits a `::warning::` pointing at
  the registry entry. It deliberately does **not** gate the release — quality
  grading is enforced by the watchdog under *Scheduled watchdogs* below.
- **[Release](.github/workflows/release.yml)** – creates a GitHub Release with
  auto-generated notes.
- **[Publish Container](.github/workflows/publish-container.yml)** – builds
  and pushes the GHCR image `ghcr.io/oliverhruby/edupage-mcp` for
  `linux/amd64` + `linux/arm64`, tagged `vX.Y.Z` and `latest`.
- **[Publish to MCP Registry](.github/workflows/publish-mcp-registry.yml)** –
  validates `server.json` against the MCP Registry schema and publishes it via
  `mcp-publisher` with GitHub OIDC (namespace `io.github.oliverhruby/*`);
  runs after PyPI/Docker publish.

### Branch-protection gates (push/PR to `main`)

Each runs on every push to `main` and on pull requests (plus the noted
schedule / manual dispatch):

- **[Quality Gates](.github/workflows/quality-gates.yml)** – `python-sanity`
  compiles `src/edupage_mcp/__init__.py` and installs the package from source;
  `docker-mcp-smoke` builds the Docker image, performs an MCP stdio handshake
  (`initialize` + `tools/list`, expecting ≥ 28 tools), and smokes the
  `streamable-http` transport both with and without `MCP_API_KEY` (auth probes
  must be rejected).
- **[Security](.github/workflows/security.yml)** – **required**; runs
  `pip-audit` over the full installed dependency tree (direct + transitive,
  OSV) and fails on any HIGH/CRITICAL CVE. Also weekly (Mon 06:00 UTC).
- **[Container Security](.github/workflows/container-security.yml)** –
  **required**; builds the image and scans it with Trivy. Fails on
  HIGH/CRITICAL findings (`ignore-unfixed: true`, `.trivyignore`) and uploads a
  SARIF report (all severities) to GitHub Security. Also weekly (Mon 07:00 UTC).
- **[Upstream Coverage](.github/workflows/upstream-coverage.yml)** –
  **required**; `coverage-drift` checks that every public `edupage-api` method
  is either wrapped in `src/edupage_mcp/__init__.py` or listed in
  `scripts/edupage_api_ignored_methods.json` with a reason.
  `latest-upstream-canary` re-checks against the newest `edupage-api` release;
  it runs only on the weekly schedule (Mon 08:00 UTC) and manual dispatch.

### Scheduled watchdogs

- **[Glama Quality Watchdog](.github/workflows/glama-quality.yml)** – every
  Monday 06:00 UTC (and on `workflow_dispatch`), reads the live Glama TDQS
  grades for `oliverhruby/edupage-mcp` via `scripts/check_glama_quality.py`:
  per-tool grade letters are parsed from the public server page; the overall
  `qualityScore` is read from the Glama API when the `GLAMA_API_KEY` secret is
  set. Any tool below grade `A`, a tool inventory that no longer matches
  `GLAMA_EXPECTED_TOOLS`, or an overall score below `4.0` keeps a
  `glama-quality` issue open (created/updated automatically, closed when green
  again). **Keep `GLAMA_EXPECTED_TOOLS` in sync with the tool surface in
  `src/edupage_mcp/__init__.py`** after adding/removing tools.

### On-demand (manual only)

- **[e2e-live](.github/workflows/e2e-ci.yml)** – the live integration suite
  (`pytest -m e2e tests/e2e`) against the real EduPage schools. Manual
  `workflow_dispatch` only; never on push/PR. Currently **dormant**:
  GitHub-hosted runners cannot reach `edupage.org` (network unreachable,
  confirmed 2026-09-10), so it needs a self-hosted runner with home-ISP
  egress — until then run the suite locally (see below).

### Live e2e suite (local)

The live integration suite (`tests/e2e/`) logs into the real schools with the
helper's EduPage account and exercises every read-only tool. It runs only on
the owner's machine (the CI copy is dormant, above):

```bash
powershell -ExecutionPolicy Bypass -File run_e2e.ps1
```

- `run_e2e.ps1` sets `EDUPAGE_E2E=1` and strips `cvcmalacky` from
  `EDUPAGE_SUBDOMAINS` — only `zssturovamalacky` and `iprskola` are approved.
  Requires the `EDUPAGE_USERNAME` / `EDUPAGE_PASSWORD` of the helper account.
- Read-only by design: `conftest.py` monkeypatches `edupage-api` write methods
  to raise, and `test_manifest.py` guarantees no write-capable tool is invoked.
- Known upstream defects are tracked as `known_error` substrings and **fail
  the suite if the message changes** (e.g. `find_student` no-such-student,
  `get_grades` `percent` UnboundLocalError in edupage-api 0.12.5). Fix in the
  wrapper or bump upstream when they drift.
- A drift report is written to `reports/e2e-report.json` (gitignored) with
  full payload previews — the local report stays on the machine.

**Regenerating child-identity fingerprints** (owner only, never on CI): the
check reads the gitignored `tests/e2e/.local.e2e.json`. When EduPage changes
or children change legitimately: (1) run the suite locally with
`.local.e2e.json` present to see the full identity diff, (2) regenerate
`tests/e2e/expected_children.fingerprint.json` with the live generator and
commit it — the commit message must not contain child identities.

### Privacy policy (summary)

This repo and its CI are **public**. Exact child identities (names + person
ids) must never be committed, printed in CI logs, or uploaded as artifacts.
Local exact-identity checks read the gitignored `tests/e2e/.local.e2e.json`;
the only CI-committed evidence is the one-way fingerprint file and a zero-data
report (`EDUPAGE_E2E_CI=1` keeps the suite dot-only and identity-free).

### Manual workflow runs

Trigger any workflow on demand:

```bash
gh workflow run security.yml --repo oliverhruby/edupage-mcp
gh workflow run quality-gates.yml --repo oliverhruby/edupage-mcp
gh workflow run container-security.yml --repo oliverhruby/edupage-mcp
gh workflow run upstream-coverage.yml --repo oliverhruby/edupage-mcp
gh workflow run glama-quality.yml --repo oliverhruby/edupage-mcp
gh workflow run e2e-ci.yml --repo oliverhruby/edupage-mcp   # live e2e (needs secrets)
```

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
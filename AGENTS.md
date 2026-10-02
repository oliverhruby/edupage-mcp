# AGENTS.md

Maintainer guidance for AI agents on this repo (not user docs — that is
README.md). This file intentionally holds **only what cannot be derived from
the code**: decisions, traps, and external context. Everything else — layout,
architecture, state model, build/verify commands, e2e operations, release
process, CI/CD workflow reference, Conventional Commits — lives in
[CONTRIBUTING.md](CONTRIBUTING.md). Read it before structural work.

## Rules — decisions, not derivable from code

1. **Thin facade.** This server wraps
   [`edupage-api`](https://github.com/EdupageAPI/edupage-api). All EduPage
   endpoint/parsing/login logic belongs upstream. When something breaks, check
   `edupage-api` first; never grow a scraping layer here.

2. **`mcp<2` is an API-compat pin, not a security pin.** `mcp` 2.x renamed
   `FastMCP` → `MCPServer` and broke the v1 API. Keep the pin and the
   `ignore: mcp >=2.0.0` block in `.github/dependabot.yml`. `edupage-api` may
   rise freely.

3. **Tool surface is a designed constraint.** Prefer a small toolset with a
   discriminating `param=` (the consolidated `login` / `get_timeline` /
   `get_roster` / `get_timetable` families are the model) over many
   near-identical tools. Adding or removing tools MUST update, in the same
   change: the `>=30` tool floor in `.github/workflows/quality-gates.yml`, the
   `GLAMA_EXPECTED_TOOLS` list in `.github/workflows/glama-quality.yml`, the
   README tool table + "What it provides" count, and the `tests/e2e` tool
   manifests (`test_manifest.py`, `test_readonly_tools.py`).

4. **Docstrings are product, not prose.** Glama grades each tool with TDQS and
   gates the server on `qualityScore` (≥ 4.0, tier A). The grader sees the
   full docstring plus one `description` per parameter, taken from its
   Google-style `Args:` block. Every tool docstring must therefore carry an
   `Args:` block and, where relevant, a cross-reference telling agents which
   sibling tool to use instead. Keep them dense — no credit for restating the
   schema. Glama's weakest dimension is `behavioralTransparency`, and the
   grader opens most verdicts with "with no annotations, the description
   carries the full burden" — so every tool also needs a `TOOL_ANNOTATIONS`
   entry (`readOnlyHint` / `destructiveHint` / `idempotentHint` /
   `openWorldHint`). The annotations say *whether* a call mutates and whether
   retrying is safe; the docstring says *what* it does. Neither replaces the
   other, and `tests/unit/test_tool_annotations.py` fails the build if a new
   tool ships without a registry entry or disagrees with its own docstring.

5. **Follow the tool skeleton in `src/edupage_mcp/__init__.py`:** `@_tool`
   functions returning `_run(go, "name")`. `_tool` keeps the fn plain-callable
   so tests introspect it without MCP installed; `_run` turns exceptions into
   `{"isError": ...}` JSON-RPC results. Reuse `_require_client`, `_serialize`,
   `_parse_date`, `_find_student` rather than reimplementing.

6. **FastMCP 1.x has no `version` param.** The server pins
   `server._mcp_server.version` from the installed dist info via `_APP_DIST` in
   `__init__.py`. Keep `_APP_DIST` equal to the `pyproject.toml` project name
   (`edupage-mcp-full`); otherwise the advertised server version is wrong.

7. **Parent accounts.** A parent's linked children come from parsing the
   school homepage (`_get_parent_children`). A failed homepage fetch **raises**
   — it is never treated as "no children", and there is deliberately no roster
   fallback. Do not "fix" this.

8. **e2e privacy (public repo + public CI).** Exact child identities (names +
   person ids) must never be committed, printed in CI logs, or uploaded as
   artifacts. CI checks only one-way SHA-256 fingerprints; local exact checks
   read the gitignored `tests/e2e/.local.e2e.json`; `EDUPAGE_E2E_CI=1` forces
   zero-data output. Only `zssturovamalacky` + `iprskola` are approved
   (`cvcmalacky` is out of scope). The `e2e-ci.yml` workflow is **dormant**
   (GitHub runners cannot reach `edupage.org`) — use `run_e2e.ps1` locally on
   the owner's machine. Fingerprint regeneration is owner-only; its commit
   message must not contain identities.

9. **Deliberate rule-1 exception: the material-player parser.**
   `get_homework_material` and `download_attachment` parse the
   `.etestPlayer(...)` payload of
   `/elearning/?cmd=MaterialPlayer&superid=…` **locally**, because
   `edupage-api` ships no homework/material reader (only the `homework` /
   `etesthw` constants in `timeline.py`). All HTTP still goes through
   upstream's `Edupage.custom_request` — no session/auth/endpoint code is
   duplicated. Next agent: check whether `edupage-api` has gained a reader; if
   it has, delete the `_hw_*` / `_html_to_text` helpers and delegate.

## Before shipping

- Sanity: `python -m py_compile src/edupage_mcp/__init__.py` and
  `python -m pytest tests/unit -q`; a manual MCP `tools/list` is the real
  verification.
- Keep tool docstrings / README / test manifests in sync with the surface
  (rules 3 + 4).
- Commit with Conventional Commits; see CONTRIBUTING.md for all operational
  detail (local setup, e2e, releases, CI/CD).
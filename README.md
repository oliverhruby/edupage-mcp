# EduPage MCP Server

<!-- mcp-name: io.github.oliverhruby/edupage-mcp -->

[![GitHub release](https://img.shields.io/github/v/tag/oliverhruby/edupage-mcp.svg?sort=semver&label=release)](https://github.com/oliverhruby/edupage-mcp/releases)
[![Glama](https://img.shields.io/badge/Glama-listed-informational)](https://glama.ai/mcp/servers/oliverhruby/edupage-mcp)
[![Quality gates](https://img.shields.io/github/actions/workflow/status/oliverhruby/edupage-mcp/quality-gates.yml.svg?label=quality%20gates)](https://github.com/oliverhruby/edupage-mcp/actions/workflows/quality-gates.yml)
[![Security](https://img.shields.io/github/actions/workflow/status/oliverhruby/edupage-mcp/security.yml.svg?label=security)](https://github.com/oliverhruby/edupage-mcp/actions/workflows/security.yml)
[![Container security](https://img.shields.io/github/actions/workflow/status/oliverhruby/edupage-mcp/container-security.yml.svg?label=container%20security)](https://github.com/oliverhruby/edupage-mcp/actions/workflows/container-security.yml)
[![Coverage drift](https://img.shields.io/github/actions/workflow/status/oliverhruby/edupage-mcp/upstream-coverage.yml.svg?label=coverage%20drift)](https://github.com/oliverhruby/edupage-mcp/actions/workflows/upstream-coverage.yml)

## Project

A Model Context Protocol (MCP) server that exposes the full functionality of the
[`edupage-api`](https://github.com/EdupageAPI/edupage-api) Python library to AI
agents such as opencode, Claude, Cursor and any other MCP client.

EduPage is a school information system used across Europe. This server lets you
query and operate a student / teacher / parent EduPage account directly from
your agent: timetables, grades, homework, substitutions, meals (including
ordering), messages, rosters, parent child-switching and more — including
**multiple schools** (e.g. two children attending different schools).

> **⚠️ Unofficial API.** Like all EduPage MCP servers, this relies on the
> community-maintained [`edupage-api`](https://github.com/EdupageAPI/edupage-api)
> library, which talks to EduPage's undocumented endpoints. Use read-only
> features freely; use the write features (`send_message`, meal ordering, child
> switching) carefully.

---

## Table of Contents

- [Why another EduPage MCP server?](#why-another-edupage-mcp-server)
- [What it provides](#what-it-provides)
- [Getting started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [1. Install](#1-install)
  - [2. Configure credentials](#2-configure-credentials)
  - [3. Register with your MCP client](#3-register-with-your-mcp-client)
- [Prompt examples](#prompt-examples)
- [Multiple schools (subdomains)](#multiple-schools-subdomains)
- [Tool reference](#tool-reference)
- [Data & safety notes](#data--safety-notes)
- [Contributing](#contributing)
- [Limitations](#limitations)
- [Support](#support)
- [License](#license)

---

## Why another EduPage MCP server?

Three EduPage MCP servers exist. All three wrap the same community-maintained
[`edupage-api`](https://github.com/EdupageAPI/edupage-api) library — none of them
reimplements EduPage's undocumented endpoints from scratch. I have **no
affiliation** with the other two; they are listed here because an honest
comparison is more useful than a marketing page.

|                          | **this project**                                                            | [mrtineu/edupage-mcp](https://github.com/mrtineu/edupage-mcp) | [mhlavac/edupage-mcp](https://github.com/mhlavac/edupage-mcp) |
| ------------------------ | -------------------------------------------------------------------------- | ---------------------------------------------------------- | --------------------------------------------------------- |
| Install                  | `uvx edupage-mcp-full`, `pip`, or one-click from the [MCP Registry](https://registry.modelcontextprotocol.io/) | `uvx edupage-mcp` (PyPI)                                     | clone + `uv sync`                                         |
| Tools                    | 29 (consolidated — see note)                                                | 13                                                          | 26                                                         |
| License                  | **MIT**                                                                     | Apache-2.0                                                  | GPL-3.0                                                    |
| Stars                    | 1                                                                           | 2                                                            | 1                                                          |
| Repo created             | 2026-09-03                                                                  | 2026-06-05                                                   | 2026-02-21                                                 |
| Last push                | 2026-09-28                                                                  | 2026-09-16                                                   | 2026-09-22                                                 |
| Test suite               | ✅ (`pytest`, run in CI)                                                    | ❌                                                            | ✅                                                          |

Two things worth stating plainly before the feature table. **This is the
newest and least-adopted of the three** — as of 2026-09-28 it is 25 days old with
a single star, while `mhlavac/edupage-mcp` predates it by over six months. And
**tool count is a poor metric**: this project deliberately folds families behind
a discriminating parameter (`get_roster(roster_type=…)`,
`get_timeline(category=…)`) rather than shipping near-identical tools, so its 29
tools cover ground that takes the others 26 or 13 separate ones. The table
below is therefore written by *capability*, not by tool name.

### Capabilities

| Capability                              | mhlavac       | mrtineu            | **this project** |
| --------------------------------------- | ------------- | ------------------ | ---------------- |
| Login — credentials                     | ✅            | ✅                 | ✅               |
| Login — portal auto-detect              | ✅            | ❌                 | ✅               |
| Login — **2FA completion**              | ❌            | ❌                 | ✅               |
| Login — existing `PHPSESSID`            | ❌            | ❌                 | ✅               |
| **Multiple schools** in one deployment  | ✅            | ❌                 | ✅               |
| Timetable — own                         | ✅            | ✅                 | ✅               |
| Timetable — by student                  | ✅ (by name)  | ❌                 | ✅ (name or id)  |
| Timetable — by class                    | ✅            | ❌                 | ✅               |
| Timetable — by **teacher**              | ❌            | ❌                 | ✅               |
| Timetable — by **classroom**            | ❌            | ❌                 | ✅               |
| Timetable — over a **date range**       | ❌            | ❌                 | ✅               |
| Next-week timetable                     | ✅            | ❌                 | ✅               |
| Grades                                  | ✅            | ✅                 | ✅               |
| Substitutions / timetable changes       | ✅            | ✅                 | ✅               |
| Missing teachers                        | ❌            | ✅ *(experimental)* | ✅               |
| Meals — read menu                       | ✅            | ✅                 | ✅               |
| Meals — **choose / sign off / rate**    | ❌            | ❌                 | ✅               |
| Send messages                           | ✅ *(open bug [#5](https://github.com/mhlavac/edupage-mcp/issues/5))* | ❌ | ✅               |
| Parent — list own children              | ✅            | ❌                 | ✅               |
| Parent — **switch session into a child**| ❌            | ❌                 | ✅               |
| Bell schedule (periods)                 | ✅            | ❌                 | ✅               |
| **Next ringing time** (live)            | ❌            | ❌                 | ✅               |
| Homework — from notifications           | ✅            | ✅                 | ✅               |
| Homework — **body text + attachments**  | ❌            | ✅                 | ❌               |
| **Download a homework file to disk**    | ❌            | ✅                 | ❌               |
| Absences                                | ✅            | ❌                 | ✅               |
| Upcoming events                         | ✅            | ❌                 | ✅               |
| School news                             | ✅            | ❌                 | ✅               |
| Assignments (tests / exams)             | ✅            | ❌                 | ✅               |
| Roster — students/teachers/classes/classrooms/subjects | ✅ | partial          | ✅               |
| Whole-school roster                     | ✅            | ❌                 | ✅               |
| **"Is the kid at school today?"**       | ✅            | ❌                 | ❌               |
| Aggregate summary                       | ✅ *(last N days)* | ❌             | ✅ *(one day)*   |
| Raw custom HTTP request                 | ❌            | ❌                 | ✅               |
| **Role detection** (parent/student/teacher) | ❌         | ❌                 | ✅               |
| Auto re-login on expired session        | ❌            | ✅                 | ✅ *(several tools)* |
| Automated tests in CI                   | lint only     | ❌                 | ✅               |

### Where the other two are genuinely better

The table above is not one-sided, and it would be dishonest to leave it there:

**[`mhlavac/edupage-mcp`](https://github.com/mhlavac/edupage-mcp)**

- **`get_school_days`** — answers "is my kid at school today / over lunch?" from
  trips, excursions and absences. **This project has no equivalent tool.**
- **Cross-school merged queries.** Its list-returning tools merge across schools,
  tag every row with a `school` field, and accept a `school=` filter. Here,
  `subdomain` selects a single school, and a few tools (for example
  `get_student_timetable`) return one result per school instead of merging.
- **Broader read surface on paper** — 26 distinct tools where this project folds
  the same ground into parameterised families. If you like one-tool-per-endpoint,
  that is a legitimate preference, not a flaw.
- Ships a `.mcp.json` for zero-config Claude Code pickup.

**[`mrtineu/edupage-mcp`](https://github.com/mrtineu/edupage-mcp)**

- **By far the deepest homework support of the three.** It parses the internal
  material-player page, so you get the assignment *body* plus a list of
  attachments, and `download_homework_file` saves the file to disk. This
  project's `get_timeline(category='homework')` — like mhlavac's — only sees what
  the timeline notification itself exposes. This is a real gap here.
- **Fails loudly on 2FA.** It detects a pending 2FA and raises a clear error
  telling you 2FA is unsupported. mhlavac discards the library's return value and
  would report success with a non-authenticated session. This project goes
  further and actually completes the flow.
- **Async-native** tool bodies, and one-command `uvx` install without a clone
  (this project matches the install story, and is additionally in the MCP
  Registry).

### What only this project does

- **2FA completion**, and login from an existing `PHPSESSID` session.
- **Parent session-switching** into a child account — and back to the parent.
- **The full canteen write surface**: `choose_meal`, `sign_off_meal`, `rate_meal`.
- **Timetables addressed by teacher or classroom**, and over a **date range**.
- **`get_next_ringing_time`** — the next bell, live.
- **`custom_request`** — a raw passthrough for any endpoint no tool covers yet.
- **Role detection**, so a parent, a student and a teacher each get the right
  behaviour from the same tool.
- **`get_day_summary`** — one call for a whole day: timetable, substitutions,
  meals, homework, absences, news and events.

### Which one should you pick?

- You want the **broadest read surface** and do not need any write operation →
  [`mhlavac/edupage-mcp`](https://github.com/mhlavac/edupage-mcp).
- You care about **homework content and files**, or want the simplest possible
  install → [`mrtineu/edupage-mcp`](https://github.com/mrtineu/edupage-mcp).
- You need **writes** (meal ordering, messages, child switching), **2FA**, or a
  **raw passthrough** → this project.

*Comparison checked against each project's source, GitHub metadata and PyPI on
2026-09-28. These projects move fast — if a row is wrong, please open an issue
rather than assuming it is deliberate.*


---

## What it provides

A single stdio MCP server exposing **29 tools** (published on PyPI as
[`edupage-mcp-full`](https://pypi.org/project/edupage-mcp-full/)):

- **Authentication** — `login` (by credentials, portal auto-detect, or a
  `PHPSESSID` cookie via `method=`), `login_all` (multi-school, one call),
  `two_factor_finish` (complete a pending 2FA), `get_subdomains` (schools
  available to the account with role/user id per school + env config, live
  subdomain discovery for parents). The former `auth_status`,
  `user_id`, `login_auto`, `login_from_session`, and
  `two_factor_check_confirmed` tools are folded into these.
- **Timetables** — `get_my_timetable`, `get_timetable` (teacher/student/class/
  classroom; `end_date` for a range, formerly `get_timetable_range`),
  `get_student_timetable` (student by name, cross-school), `get_next_week_timetable`,
  `get_next_ringing_time`, `get_periods`, `get_school_year`
- **Students** — `find_student` (name → person_id, cross-school),
  `get_student_timetable` (cross-school, role-aware), `scan_students`
  (auto-discover all students across schools), `get_my_students` (classmates or
  school-wide for parents), `switch_to_student` (by id **or** name, parent only),
  `switch_to_parent`, `clear_student_cache` (force refresh cached student lists)
- **Schools** — `get_subdomains` (subdomains available to the account — live-discovered for parents, limited by `EDUPAGE_SUBDOMAINS` when set — plus session state per school)
- **Grades** — `get_grades`
- **Timeline / notifications** — `get_timeline` (`category=` for homework,
  assignments, absences, events, news, or full history since a date)
- **Substitutions** — `get_timetable_changes`, `get_missing_teachers`
- **Meals** — `get_meals`, `choose_meal`, `sign_off_meal`, `rate_meal`
- **Day summaries** — `get_day_summary` (one call: timetable, substitutions,
  missing teachers, grades, meals, homework, assignments, absences, news,
  events, notifications for a date — "what happened yesterday at school" in a
  single round trip; each section is isolated so one failure doesn't kill the
  report). Includes an OpenCode skill (`school-day-summary`) for human-readable
  formatting in OpenCode; other clients use the raw JSON directly.
- **Rosters** — `get_roster` (`roster_type=` for students, all students,
  teachers, classes, classrooms, or subjects), `get_my_students`
- **Actions** — `send_message`, `switch_to_student`, `switch_to_parent`, `custom_request`

---

## Getting started

You need an MCP-capable client (opencode, Claude Desktop, Cursor, etc.).

### 1. Install

If you are using an AI coding client, a simple prompt is often enough to get
started, for example: "Install the EduPage MCP as described in this GitHub
repository oliverhruby/edupage-mcp". Most MCP-capable clients can then guide
you through the available setup options.

**Option A — from MCP Registry (recommended, one-click in VS Code / GitHub Copilot)**

The server is listed in the [MCP Registry](https://registry.modelcontextprotocol.io/).
In VS Code or GitHub Copilot, search for "EduPage MCP" and install with one click.
Or use the direct deeplink: `mcp://install/io.github.oliverhruby/edupage-mcp`

**Option B — from PyPI**

Use this for normal usage with a released version.

Requirements: `uv` for `uvx`, or Python **3.10+** for `pip`.

```bash
uvx edupage-mcp-full
# or, if you prefer pip (into whatever environment your MCP client uses):
pip install edupage-mcp-full
```

`uvx` runs the package without a persistent install. If `uvx` is unavailable,
install `uv` first (`pip install uv` or `winget install astral-sh.uv`).

**Option C — from GitHub (latest source)**

Use this if you want the latest changes before a PyPI release.

Requirements: `uv` for `uvx`, or Python **3.10+** for `pip`.

```bash
uvx --from "git+https://github.com/oliverhruby/edupage-mcp.git" edupage-mcp-full
# or
pip install "git+https://github.com/oliverhruby/edupage-mcp.git"
```

**Option D — Docker**

Use this for an isolated container runtime.

Requirements: Docker.

Pull a prebuilt image (recommended):

```bash
docker pull ghcr.io/oliverhruby/edupage-mcp:latest

docker run --rm -i \
  -e EDUPAGE_USERNAME=your_username \
  -e EDUPAGE_PASSWORD=your_password \
  ghcr.io/oliverhruby/edupage-mcp:latest
```

Version tags are also available (for example `v0.4.0`) if you prefer pinned
images.

Build locally from source (fallback):

```bash
docker build -t edupage-mcp-full .

docker run --rm -i \
  -e EDUPAGE_USERNAME=your_username \
  -e EDUPAGE_PASSWORD=your_password \
  edupage-mcp-full
```

The container uses the same environment variables described in
[Configure credentials](#2-configure-credentials). It also includes a
`HEALTHCHECK` (stdio process liveness by default; local TCP check in HTTP
transport modes).

For HTTP transports, set optional runtime vars:

- `MCP_TRANSPORT`: `stdio` (default), `sse`, or `streamable-http`
- `MCP_HOST`: bind host (default `127.0.0.1`)
- `MCP_PORT`: bind port (default `8000`)
- `MCP_API_KEY`: optional bearer token for HTTP auth

When `MCP_API_KEY` is set, HTTP requests must include `Authorization: Bearer <key>`.
If `MCP_API_KEY` is not set, HTTP endpoints are unauthenticated. For production,
prefer proper authentication and TLS via a reverse proxy or API gateway.

> `pyproject.toml` pins `mcp<2` (the stable FastMCP v1 API). `mcp 2.x` renamed
> `FastMCP` to `MCPServer` and changed the API surface; this server targets the
> FastMCP v1 API for simplicity and stability.

**Option C — development from source**

Use this if you are contributing or debugging locally.

Requirements: Python **3.10+**.

```bash
git clone https://github.com/oliverhruby/edupage-mcp.git
cd edupage-mcp
uv sync               # or: python -m venv .venv && .venv/bin/python -m pip install -e .
uv run edupage-mcp-full
```

**Option D — Docker**

Use this for an isolated container runtime.

Requirements: Docker.

Pull a prebuilt image (recommended):

```bash
docker pull ghcr.io/oliverhruby/edupage-mcp:latest

docker run --rm -i \
  -e EDUPAGE_USERNAME=your_username \
  -e EDUPAGE_PASSWORD=your_password \
  ghcr.io/oliverhruby/edupage-mcp:latest
```

Version tags are also available (for example `v0.4.0`) if you prefer pinned
images.

Build locally from source (fallback):

```bash
docker build -t edupage-mcp-full .

docker run --rm -i \
  -e EDUPAGE_USERNAME=your_username \
  -e EDUPAGE_PASSWORD=your_password \
  edupage-mcp-full
```

The container uses the same environment variables described in
[Configure credentials](#2-configure-credentials). It also includes a
`HEALTHCHECK` (stdio process liveness by default; local TCP check in HTTP
transport modes).

For HTTP transports, set optional runtime vars:

- `MCP_TRANSPORT`: `stdio` (default), `sse`, or `streamable-http`
- `MCP_HOST`: bind host (default `127.0.0.1`)
- `MCP_PORT`: bind port (default `8000`)
- `MCP_API_KEY`: optional bearer token for HTTP auth

When `MCP_API_KEY` is set, HTTP requests must include `Authorization: Bearer <key>`.
If `MCP_API_KEY` is not set, HTTP endpoints are unauthenticated. For production,
prefer proper authentication and TLS via a reverse proxy or API gateway.

> `pyproject.toml` pins `mcp<2` (the stable FastMCP v1 API). `mcp 2.x` renamed
> `FastMCP` to `MCPServer` and changed the API surface; this server targets the
> FastMCP v1 API for simplicity and stability.

### 2. Configure credentials

Either set environment variables **or** pass credentials to `login` (see
[Prompt examples](#prompt-examples)).

```bash
# Windows (persistent, per-user)
setx EDUPAGE_USERNAME "your_username"
setx EDUPAGE_PASSWORD "your_password"
setx EDUPAGE_SUBDOMAINS "s1,s2,s3"       # optional: multiple schools (auto-login + discovery)

# macOS / Linux
export EDUPAGE_USERNAME="your_username"
export EDUPAGE_PASSWORD="your_password"
export EDUPAGE_SUBDOMAINS="s1,s2,s3"     # optional
```

**Single school?** Just set `EDUPAGE_USERNAME` + `EDUPAGE_PASSWORD`. The server
auto-discovers your school via the EduPage portal on startup — no subdomain needed.

**Multiple schools?** Add `EDUPAGE_SUBDOMAINS` (comma-separated). The server
logs into all of them on startup with your shared credentials.

### 3. Register with your MCP client

**opencode** — add to `~/.config/opencode/opencode.json` (or `opencode.jsonc`):

```jsonc
{
  "mcp": {
    "edupage": {
      "type": "local",
      "enabled": true,
      "command": ["uvx", "edupage-mcp-full"],
      "env": {
        "EDUPAGE_USERNAME": "{env:EDUPAGE_USERNAME}",
        "EDUPAGE_PASSWORD": "{env:EDUPAGE_PASSWORD}",
        "EDUPAGE_SUBDOMAINS": "{env:EDUPAGE_SUBDOMAINS}"
      }
    }
  }
}
```

> Put credentials in your shell/environment (or a `.env`) and reference them with
> `{env:VAR}`, or hardcode them under `env:` directly. `uvx` will auto-provision
> the package the first time; it must be on your `PATH`.

**Claude Desktop / Cursor** — use `claude_desktop_config.json` /
`.mcp.json` with a `mcpServers` entry in the standard shape, pointing
`command`/`args` at the venv python and the `edupage_mcp.py` path, plus an
`env` block with your credentials.

After editing client config, **restart the client** so the MCP server is loaded.

---

## Prompt examples

| User prompt | Likely tool call(s) | Expected response |
|---|---|---|
| "Are we connected and logged in?" | `get_subdomains` | Available school subdomains (live-discovered for parents), active school/subdomain, env config, and login state per school. |
| "What classes do I have today?" | `get_my_timetable` | A short timetable summary for today. |
| "Show me the 9.A schedule for 2026-09-10" | `get_timetable target_type="class" target_id="9.A" date_str="2026-09-10"` | Class timetable for that date. |
| "What grades do I have this term?" | `get_grades term="FIRST" year=2026` | Subject-by-subject grade overview for the selected term/year. |
| "Any substitutions today?" | `get_timetable_changes` | Changes, cancellations, and replacements for today. |
| "What is for lunch and order option 2 for tomorrow" | `get_meals` → `choose_meal date_str="2026-09-10" meal_type="lunch" number=2` | Meal menu and order confirmation (or a clear error if unavailable). |
| "Find Student A's timetable for tomorrow" | `get_student_timetable name="Student A" date_str="2026-09-10"` | Student A's timetable; if found in multiple schools, one result per school. |
| "List teachers and send a hello to Teacher456" | `get_roster roster_type="teachers"` → `send_message recipient_id="Teacher456" body="Hello!"` | Teacher list plus message sent confirmation. |
| **"What happened at school yesterday for my kids?"** | `get_day_summary date_str="2026-09-09"` (discovery index) → `get_day_summary date_str="2026-09-09" name="Student A" subdomain="school-a"` → `... name="Student B" subdomain="school-b"` | **Discovery-first**: the no-name call lists each child per school; then one complete daily report call per child (timetable, substitutions, missing teachers, grades, meals, homework, assignments, absences, news, events, notifications). Keeps each response small and avoids mixing schools/students. |
| **"How was school today for Student A?"** | `get_day_summary name="Student A"` (defaults to today) | Human-readable summary via the bundled OpenCode skill `school-day-summary`. |

---

## Multiple schools & automatic student discovery

Each subdomain (school) keeps its **own** logged-in session. There are two ways
to log in to several schools at once:

**A) Automatic on startup (recommended).** Set `EDUPAGE_SUBDOMAINS` (a
comma-separated list) plus the shared `EDUPAGE_USERNAME` / `EDUPAGE_PASSWORD` —
the server logs into all of them when it launches, so every tool is immediately
ready and students are discoverable across all schools with **no login call and
no student→school mapping**:

```bash
setx EDUPAGE_SUBDOMAINS "school1,school2,school3"   # Windows
export EDUPAGE_SUBDOMAINS="school1,school2,school3" # macOS / Linux
```

```text
get_subdomains   # lists school1, school2, school3 (logged in, with role)
scan_students      # discovers Student A and Student B across those schools
get_student_timetable name="Student A"   # is found at school1 AND school2
```

**B) On demand with `login_all`.** Authenticate several schools at once, then pass
`subdomain` to any data tool (it defaults to the last active subdomain when
omitted):

```text
login_all subdomains="school1,school2" usernames="u1,u2" passwords="p1,p2"

get_my_timetable subdomain="school1"
get_my_timetable subdomain="school2"
get_subdomains         # shows all logged-in subdomains + which is active
```

You can also call `login` once per school to add/lookup sessions incrementally.

> **Single school?** No `EDUPAGE_SUBDOMAINS` needed — the server auto-discovers
> your school via the portal on startup. For two or more schools, set
> `EDUPAGE_SUBDOMAINS` (auto-login) or use `login_all` / repeated `login` calls.

---

## Students by name (e.g. "timetable for Student A")

Because the server **auto-discovers students across the configured
`EDUPAGE_SUBDOMAINS`** (or every logged-in school when the variable is unset),
you don't need to know or state which school a student is in. Just ask for the
timetable by name and the server searches every school in scope:

```text
"timetable for Student A"  ->  get_student_timetable name="Student A"
```

`get_student_timetable` (with no `subdomain`):

1. searches **every school in the discovery scope** — the configured
   `EDUPAGE_SUBDOMAINS`, or all logged-in schools when unset — for a student
   whose first/last/full name matches (`scan_students` does just the discovery
   step),
2. for each school where the student is found, switches to the student account if
   you're logged in as a parent, returns that student's timetable for the date, and
   switches back to the parent account afterwards,
3. returns **one result per school**.

A student attending **more than one school** (e.g. Student at `school1` +
`school2`) therefore yields a list of two per-school timetables — separate
results, never merged. This is the built-in replacement for maintaining a
manual "Student → school1" mapping: with `EDUPAGE_SUBDOMAINS` set, discovery is
fully automatic.

---

## Tool reference

| Tool | Description | Writes? |
|---|---|---|
| `login` | Log in with username/password; `method="credentials"` (default), `method="auto"` (portal auto-detect, formerly `login_auto`), or `method="session"` with a `PHPSESSID` cookie (formerly `login_from_session`). Env vars supported. | ✅ session |
| `login_all` | Log in to multiple schools in one call | ✅ session |
| `two_factor_finish` | Finish a pending 2FA login (email/app `code` or `poll_seconds` device confirmation; formerly `two_factor_check_confirmed` + `two_factor_finish`) | ✅ session |
| `get_subdomains` | Subdomains available to the account (live-discovered for parents; limited to `EDUPAGE_SUBDOMAINS` when set) + role/user id per school, active subdomain, failed logins, env config (formerly `auth_status`, `user_id`) |  |
| `get_school_year` | Current school year |  |
| `get_my_timetable` | Logged-in user's timetable for a date |  |
| `get_timetable` | Timetable of a teacher/student/class/classroom; `end_date` for a daily range (formerly `get_timetable_range`) |  |
| `get_student_timetable` | Student's timetable by name or id (role-aware, cross-school) | ✅ session |
| `get_next_week_timetable` | Mon–Fri timetable for next week |  |
| `get_next_ringing_time` | Next bell (break/lesson) at a given time |  |
| `get_periods` | Bell schedule (period start/end times) |  |
| `get_grades` | Grades, optionally by year & term |  |
| `get_timeline` | Timeline notifications, one `category=` at a time: `recent`, `history` (since `date_from`), `homework`, `assignments`, `absences`, `events`, `news` (formerly `get_notifications`, `get_notification_history`, `get_homework`, `get_assignments`, `get_absences`, `get_upcoming_events`, `get_news`) |  |
| `get_timetable_changes` | Substitutions / timetable changes for a date |  |
| `get_missing_teachers` | Teachers missing on a date |  |
| `get_day_summary` | One-call daily report (timetable, substitutions, teachers, grades, meals incl. breakfast/dinner when published, homework, assignments, absences, news, events, notifications) for a date; student by name/id (role-aware). **Discovery-first**: parent without `name`/`student_id` returns a lightweight per-school student index (`mode:"discovery"`); pass `full=True` to build full reports for every child. Bundles OpenCode skill `school-day-summary` for human-readable output. |  |
| `get_meals` | Meal menu (all 5 slots: breakfast, snack, lunch, afternoon snack, dinner) |  |
| `choose_meal` | Order a meal | ✅ |
| `sign_off_meal` | Cancel an ordered meal | ✅ |
| `rate_meal` | Rate a meal (quality/quantity) | ✅ |
| `get_roster` | One `roster_type=` at a time: `students` (logged-in user's class), `all_students` (whole school, short list), `teachers`, `classes`, `classrooms`, `subjects` (formerly `get_students`, `get_all_students`, `get_teachers`, `get_classes`, `get_classrooms`, `get_subjects`) |  |
| `get_my_students` | Students visible to the logged-in account (one school) |  |
| `find_student` | Look up a student's person_id by name (cross-school) |  |
| `scan_students` | Auto-discover students across the configured `EDUPAGE_SUBDOMAINS` (or all logged-in schools when unset) |  |
| `clear_student_cache` | Clear cached student rosters (one school or all schools) | ✅ cache |
| `get_subdomains` | Available school subdomains + role/session per school |  |
| `send_message` | Send a message to a user | ✅ |
| `switch_to_student` | Switch to a student account by id or name (parent only) | ✅ session |
| `switch_to_parent` | Switch back to the parent account | ✅ session |
| `custom_request` | Raw request through the active session (GET/POST) | ✅ |

---

## Data & safety notes

- Most tools are **read-only**. The ones marked **Writes? ✅** mutate EduPage
  state (sent messages, ordered meals, switched accounts). Use them with care.
- `get_timeline` categories `homework`, `assignments`, `absences`, `events` and
  `news` derive their data from the **timeline notifications** — if the school
  doesn't push certain event types, those categories may return empty lists.
- `get_missing_teachers` is marked **experimental** upstream (parses HTML from
  the substitution page) and can raise if a teacher's name no longer matches.
- Meal `rate_meal` and ordering depend on the school publishing menus with the
  matching identifiers; not all schools expose ratings.
- `get_meals` first tries the per-student meal-ordering endpoint (needed for
  ordering/ratings). When a school doesn't enable that, it falls back to the
  school's **public canteen menu widget** (`/menu/?wid=menu_CanteenMenu_1`).
  All five slots (breakfast/snack/lunch/afternoon_snack/dinner) are always
  returned; slots the school doesn't publish are ``None``.

---

## Skills

The package includes an **OpenCode skill** (`school-day-summary`) at
`<site-packages>/edupage_mcp/skills/school-day-summary/SKILL.md`. It teaches
OpenCode agents how to turn `get_day_summary` JSON into a human-readable daily
school report.

**OpenCode only:** To register it:
```bash
mkdir -p ~/.config/opencode/skills/school-day-summary
cp <site-packages>/edupage_mcp/skills/school-day-summary/SKILL.md \
   ~/.config/opencode/skills/school-day-summary/SKILL.md
```
Restart OpenCode; the agent can then answer *"what happened at school yesterday
for my kids?"* by calling `get_day_summary` per child.

**Other MCP clients (Copilot, Claude, Cursor, etc.)** — call `get_day_summary`
directly; they receive the full structured JSON. Formatting is client-specific
(no skill system in the MCP protocol).

---

## Contributing

Contributor and maintainer guidance is in [CONTRIBUTING.md](CONTRIBUTING.md).

- Contribution workflow and local setup
- Architecture and implementation details
- Release process (PyPI, GitHub Releases, GHCR)
- CI quality gates and upstream coverage drift checks

---

## Limitations

- **Unofficial/read-mostly by design.** EduPage can change its endpoints at any
  time; reliability ultimately depends on `edupage-api`, not this wrapper.
- **No CAPTCHA bypass.** If EduPage presents a CAPTCHA during login, log in via
  browser first, then use `login method="session"` with the resulting `PHPSESSID`.
- **2FA requires human interaction** (approve on device or provide a code).
- **Parent/teacher accounts** are only partially verified upstream; some parent
  methods are best-effort.
- The auth session lives for the lifetime of the MCP server process; restarting
  the client means logging in again.
- Cross-school student discovery depends on being logged into all relevant
  schools (via `EDUPAGE_SUBDOMAINS`, `login_all`, or repeated `login` calls).
  If a school is not logged in, that student's results from that school cannot
  be discovered.

---

## Support

If you like this project and want to support or request a feature, send me a
beer, it keeps my mind relaxed and ideas will come :-)

[![Support via PayPal](https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif)](https://www.paypal.me/oliverhruby/)

---

## License

[MIT](LICENSE) © Oliver Hrubý

This project is **not affiliated with or endorsed by** Ascora (EduPage) or by
the authors of `edupage-api`. EduPage is a registered trademark of its
respective owner(s).

# edupage-mcp

[![PyPI version](https://img.shields.io/pypi/v/edupage-mcp-full.svg)](https://pypi.org/project/edupage-mcp-full/)
[![Downloads](https://img.shields.io/pypi/dm/edupage-mcp-full.svg)](https://pypi.org/project/edupage-mcp-full/)
[![Quality gates](https://img.shields.io/github/actions/workflow/status/oliverhruby/edupage-mcp/quality-gates.yml.svg)](https://github.com/oliverhruby/edupage-mcp/actions/workflows/quality-gates.yml)
[![Security](https://img.shields.io/github/actions/workflow/status/oliverhruby/edupage-mcp/security.yml.svg)](https://github.com/oliverhruby/edupage-mcp/actions/workflows/security.yml)
[![Container security](https://img.shields.io/github/actions/workflow/status/oliverhruby/edupage-mcp/container-security.yml.svg)](https://github.com/oliverhruby/edupage-mcp/actions/workflows/container-security.yml)
[![Coverage drift](https://img.shields.io/github/actions/workflow/status/oliverhruby/edupage-mcp/upstream-coverage.yml.svg)](https://github.com/oliverhruby/edupage-mcp/actions/workflows/upstream-coverage.yml)

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

Two other EduPage MCP servers already exist:

- [`mrtineu/edupage-mcp`](https://github.com/mrtineu/edupage-mcp) — also
  published on PyPI as [`edupage-mcp`](https://pypi.org/project/edupage-mcp/)
- [`mhlavac/edupage-mcp`](https://github.com/mhlavac/edupage-mcp)

Both are good and I have **no affiliation** with them — they are simply
referenced here for honest comparison. They primarily focus on the **read-only**
surface of the API.

This project deliberately goes further:

| Capability | mhlavac | mrtineu (PyPI) | **this project** |
|---|---|---|---|
| Timetables (own + any teacher/class/room) | ✅ | ✅ | ✅ |
| Grades (all / by term & year) | ✅ | ✅ | ✅ |
| Substitutions / timetable changes | ✅ | ✅ | ✅ |
| Meals — **read menu** | ✅ | ✅ | ✅ |
| Meals — **choose / sign-off / rate** | ❌ | ❌ | ✅ |
| Send messages (`send_message`) | ✅ | ❌ | ✅ |
| Parent **student switching** (switch to/from student) | partial (list) | ❌ | ✅ |
| **2FA** login flow (device + email code) | ❌ | ❌ | ✅ |
| Login via **session id** (`PHPSESSID`) | ❌ | ❌ | ✅ |
| Portal login (`login_auto`) | ✅ | ❌ | ✅ |
| Next ringing time / bell schedule | ❌ | ❌ | ✅ |
| Raw session **custom request** | ❌ | ❌ | ✅ |
| **Multiple schools** (auto-login + discovery) | ❌ | ❌ | ✅ |
| **Role-aware** (parent / student / teacher) | ❌ | ❌ | ✅ |

**Key differentiators:**

- **Multi-school automatic discovery.** Set `EDUPAGE_SUBDOMAINS` with one shared
  login and the server auto-discovers students across all schools — no need to
  maintain a manual "Viktor → school A, Tamara → school B" mapping. A student at
  two schools (e.g. Tamara at `iprskola` + `cvcmalacky`) is found automatically
  with separate per-school results.
- **Role-aware tools.** The server detects whether you're a parent, student, or
  teacher at each school and behaves accordingly — `get_student_timetable`
  switches to the student account for parents, returns direct timetables for
  students. No tool duplication.
- **Full write surface.** Meal ordering/rating, message sending, student switching
  — the other servers don't cover these.

---

## What it provides

A single stdio MCP server exposing **44 tools** (published on PyPI as
[`edupage-mcp-full`](https://pypi.org/project/edupage-mcp-full/)):

- **Authentication** — `login`, `login_auto`, `login_all`, `login_from_session`,
  `two_factor_check_confirmed`, `two_factor_finish`, `auth_status`, `user_id`
- **Timetables** — `get_my_timetable`, `get_timetable` (teacher/student/class/
  classroom), `get_student_timetable` (student by name, cross-school),
  `get_next_week_timetable`, `get_next_ringing_time`, `get_periods`, `school_year`
- **Students** — `find_student` (name → person_id, cross-school),
  `get_student_timetable` (cross-school, role-aware), `scan_students`
  (auto-discover all students across schools), `get_my_students` (classmates or
  school-wide for parents), `switch_to_student` (by id **or** name, parent only),
  `switch_to_parent`, `clear_student_cache` (force refresh cached student lists)
- **Schools** — `get_schools` (logged-in schools with role per school)
- **Grades** — `get_grades`
- **Notifications / timeline** — `get_notifications`, `get_notification_history`,
  `get_homework`, `get_assignments`, `get_absences`, `get_upcoming_events`, `get_news`
- **Substitutions** — `get_timetable_changes`, `get_missing_teachers`
- **Meals** — `get_meals`, `choose_meal`, `sign_off_meal`, `rate_meal`
- **Rosters** — `get_students`, `get_all_students`, `get_teachers`, `get_classes`,
  `get_classrooms`, `get_subjects`, `get_my_students`
- **Actions** — `send_message`, `switch_to_student`, `switch_to_parent`, `custom_request`

---

## Getting started

You need an MCP-capable client (opencode, Claude Desktop, Cursor, etc.).

### 1. Install

If you are using an AI coding client, a simple prompt is often enough to get
started, for example: "Install the EduPage MCP as described in this GitHub
repository oliverhruby/edupage-mcp". Most MCP-capable clients can then guide
you through the available setup options.

**Option A — from PyPI (recommended)**

Use this for normal usage with a released version.

Requirements: `uv` for `uvx`, or Python **3.10+** for `pip`.

```bash
uvx edupage-mcp-full
# or, if you prefer pip (into whatever environment your MCP client uses):
pip install edupage-mcp-full
```

`uvx` runs the package without a persistent install. If `uvx` is unavailable,
install `uv` first (`pip install uv` or `winget install astral-sh.uv`).

**Option B — from GitHub (latest source)**

Use this if you want the latest changes before a PyPI release.

Requirements: `uv` for `uvx`, or Python **3.10+** for `pip`.

```bash
uvx --from "git+https://github.com/oliverhruby/edupage-mcp.git" edupage-mcp-full
# or
pip install "git+https://github.com/oliverhruby/edupage-mcp.git"
```

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
| "Are we connected and logged in?" | `auth_status` | Connected status, active school/subdomain, and login state per school. |
| "What classes do I have today?" | `get_my_timetable` | A short timetable summary for today. |
| "Show me the 9.A schedule for 2026-09-10" | `get_timetable target_type="class" target_id="9.A" date_str="2026-09-10"` | Class timetable for that date. |
| "What grades do I have this term?" | `get_grades term="FIRST" year=2026` | Subject-by-subject grade overview for the selected term/year. |
| "Any substitutions today?" | `get_timetable_changes` | Changes, cancellations, and replacements for today. |
| "What is for lunch and order option 2 for tomorrow" | `get_meals` → `choose_meal date_str="2026-09-10" meal_type="lunch" number=2` | Meal menu and order confirmation (or a clear error if unavailable). |
| "Find Viktor's timetable for tomorrow" | `get_student_timetable name="Viktor" date_str="2026-09-10"` | Viktor's timetable; if found in multiple schools, one result per school. |
| "List teachers and send a hello to Teacher456" | `get_teachers` → `send_message recipient_id="Teacher456" body="Hello!"` | Teacher list plus message sent confirmation. |

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
setx EDUPAGE_SUBDOMAINS "zssturovamalacky,iprskola,cvcmalacky"   # Windows
export EDUPAGE_SUBDOMAINS="zssturovamalacky,iprskola,cvcmalacky" # macOS / Linux
```

```text
get_schools        # lists zssturovamalacky, iprskola, cvcmalacky (logged in, with role)
scan_students      # discovers Viktor and Tamara across those schools
get_student_timetable name="Tamara"   # is found at iprskola AND cvcmalacky
```

**B) On demand with `login_all`.** Authenticate several schools at once, then pass
`subdomain` to any data tool (it defaults to the last active subdomain when
omitted):

```text
login_all subdomains="zssturovamalacky,iprskola" usernames="u1,u2" passwords="p1,p2"

get_my_timetable subdomain="zssturovamalacky"
get_my_timetable subdomain="iprskola"
auth_status          # shows all logged-in subdomains + which is active
```

You can also call `login` once per school to add/lookup sessions incrementally.

> **Single school?** No `EDUPAGE_SUBDOMAINS` needed — the server auto-discovers
> your school via the portal on startup. For two or more schools, set
> `EDUPAGE_SUBDOMAINS` (auto-login) or use `login_all` / repeated `login` calls.

---

## Students by name (e.g. "timetable for Viktor")

Because the server **auto-discovers students across all logged-in schools**, you
don't need to know or state which school a student is in. Just ask for the
timetable by name and the server searches every school it's logged into:

```text
"timetable for Viktor"  ->  get_student_timetable name="Viktor"
```

`get_student_timetable` (with no `subdomain`):

1. searches **every logged-in school** for a student whose first/last/full name
   matches (`scan_students` does just the discovery step),
2. for each school where the student is found, switches to the student account if
   you're logged in as a parent, returns that student's timetable for the date, and
   switches back to the parent account afterwards,
3. returns **one result per school**.

A student attending **more than one school** (e.g. Tamara at `iprskola` +
`cvcmalacky`) therefore yields a list of two per-school timetables — separate
results, never merged. This is the built-in replacement for maintaining a
manual "Viktor → zsskola1" mapping: with `EDUPAGE_SUBDOMAINS` set, discovery is
fully automatic.

---

## Tool reference

| Tool | Description | Writes? |
|---|---|---|
| `login` | Log in with username/password/subdomain (env vars supported) | ✅ session |
| `login_auto` | Log in via the EduPage portal (auto-detect school) | ✅ session |
| `login_all` | Log in to multiple schools in one call | ✅ session |
| `login_from_session` | Create a session from an existing `PHPSESSID` cookie | ✅ session |
| `two_factor_check_confirmed` | Check if 2FA was approved on a device |  |
| `two_factor_finish` | Finish 2FA (email/app code or device confirmation) | ✅ session |
| `auth_status` | Which subdomains are logged in + active one |  |
| `user_id` | Logged-in user id |  |
| `school_year` | Current school year |  |
| `get_my_timetable` | Logged-in user's timetable for a date |  |
| `get_timetable` | Timetable of a teacher/student/class/classroom |  |
| `get_student_timetable` | Student's timetable by name or id (role-aware, cross-school) | ✅ session |
| `get_next_week_timetable` | Mon–Fri timetable for next week |  |
| `get_next_ringing_time` | Next bell (break/lesson) at a given time |  |
| `get_periods` | Bell schedule (period start/end times) |  |
| `get_grades` | Grades, optionally by year & term |  |
| `get_notifications` | Timeline notifications |  |
| `get_notification_history` | Timeline notifications since a date |  |
| `get_homework` | Homework from the timeline |  |
| `get_assignments` | Homework/tests/exams from the timeline |  |
| `get_absences` | Absence records from the timeline |  |
| `get_upcoming_events` | Trips/excursions/meetings/holidays |  |
| `get_news` | School news |  |
| `get_timetable_changes` | Substitutions / timetable changes for a date |  |
| `get_missing_teachers` | Teachers missing on a date |  |
| `get_meals` | Meal menu (snack/lunch/afternoon snack) |  |
| `choose_meal` | Order a meal | ✅ |
| `sign_off_meal` | Cancel an ordered meal | ✅ |
| `rate_meal` | Rate a meal (quality/quantity) | ✅ |
| `get_students` | Students in the logged-in user's class |  |
| `get_all_students` | All students in the school (short list) |  |
| `get_teachers` | All teachers |  |
| `get_classes` | All classes |  |
| `get_classrooms` | All classrooms |  |
| `get_subjects` | All subjects |  |
| `get_my_students` | Students visible to the logged-in account (one school) |  |
| `find_student` | Look up a student's person_id by name (cross-school) |  |
| `scan_students` | Auto-discover students across **all** logged-in schools |  |
| `clear_student_cache` | Clear cached student rosters (one school or all schools) | ✅ cache |
| `get_schools` | List logged-in schools + role per school |  |
| `send_message` | Send a message to a user | ✅ |
| `switch_to_student` | Switch to a student account by id or name (parent only) | ✅ session |
| `switch_to_parent` | Switch back to the parent account | ✅ session |
| `custom_request` | Raw request through the active session (GET/POST) | ✅ |

---

## Data & safety notes

- Most tools are **read-only**. The ones marked **Writes? ✅** mutate EduPage
  state (sent messages, ordered meals, switched accounts). Use them with care.
- `get_homework`, `get_assignments`, `get_absences`, `get_upcoming_events` and
  `get_news` derive their data from the **timeline notifications** — if the
  school doesn't push certain event types, those tools may return empty lists.
- `get_missing_teachers` is marked **experimental** upstream (parses HTML from
  the substitution page) and can raise if a teacher's name no longer matches.
- Meal `rate_meal` and ordering depend on the school publishing menus with the
  matching identifiers; not all schools expose ratings.

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
  browser first, then use `login_from_session` with the resulting `PHPSESSID`.
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

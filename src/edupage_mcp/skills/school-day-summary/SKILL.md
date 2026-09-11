---
name: school-day-summary
description: Build a "what happened at school that day" report for a child/student for yesterday, today, or an upcoming date — combining timetable, substitutions, missing teachers, meals, grades received, homework, tests/exams, absences, news, events, and timeline notifications into one summary. Use when the user asks for a daily summary, "what happened yesterday", "what is coming tomorrow", "how was school", or a combined per-day overview. Pairs with the `get_day_summary` tool from the edupage-mcp-full server.
---

# School day summary

Use this when the user wants a single-day, cross-cutting overview of school
activity for one student (or the logged-in account) — for example "what
happened yesterday for my child", "what does today look like", or "what is coming
tomorrow".

## Workflow

1. **Determine the date.** Ask yourself what day the user means:
   - nothing specified → today
   - "yesterday" / "today" / "tomorrow" → those dates
   - a concrete `YYYY-MM-DD` or "Monday" → use it
   If the user says something like "this week" or "last week", run the summary
   per school day (Mon-Fri) or per relevant date rather than a single day.

2. **Determine who.** Prefer the student the conversation is about. If a
   student is not already identified and the account is a parent, ask which
   child when ambiguous (e.g. "Student A" or "Student B"). When no student is named,
   report on one student at a time — never a whole class or every child at
   every school in a single report.

3. **Discover first, then report per student.** Parent accounts: call
   `get_day_summary` *without* `name`/`student_id` to get a lightweight
   per-school discovery index (it returns `mode: "discovery"`). Pick the child
   (by `name` or `student_id`) and school (`subdomain`) you actually want, then
   call `get_day_summary` once **per child** with:
   - `date_str` = the target date
   - `name` = the child's name (or `student_id`)
   - `subdomain` = that child's school (scopes the search; keeps the response small)

   This returns every section for that one student in a single round trip:
   `timetable`, `substitutions`, `missing_teachers`, `grades`, `meals`,
   `homework`, `assignments`, `absences`, `news`, `events`, `notifications`.

   Do **not** call `get_day_summary` without `name` expecting full reports — by
   default it only lists students (use `full=True` for all-children reports,
   only if truly needed).

   If a bare first name returns no result, retry with the **full name**
   (e.g. `name="Student A"`): some school rosters expose only initials, so
   the matcher needs first + last name.

4. **Fall back to individual tools if needed.** If `get_day_summary` is not
   available (older server), compose from the standard tools:
   `get_student_timetable` (or `get_my_timetable`), `get_timetable_changes`,
   `get_missing_teachers`, `get_grades`, `get_meals`, `get_homework`,
   `get_assignments`, `get_absences`, `get_upcoming_events`, `get_news`,
   `get_notifications`.

5. **Per-section handling.** Each section has `"ok": true/false`. Treat a
   `false` section as "no data / not available" for that school — do not
   fabricate content, and do not fail the whole report. Sections that are `ok`
   but empty just get omitted or listed as "nothing".

## Output format

Render a concise markdown report titled with the date (e.g.
`### Štvrtok 2026-09-10 — Student A`), then:

- **Rozvrh (Lessons)** — subject, time, room/teacher if present.
- **Zastupovanie / zmeny (Substitutions)** — only if there are changes; say
  "bez zmien" otherwise.
- **Chýbajúci učitelia (Missing teachers)** — teachers absent that day.
- **Jedálny lístok (Meals)** — snack/lunch/afternoon snacks with main foods.
- **Známky (Grades)** — grades received that day.
- **Domáce úlohy / písomky (Homework / tests)** — assignments for that date.
- **Udalosti / správy / absencie (Events / news / absences)** — anything notable.

Use the user's language for the narrative (Slovak if they write in Slovak) and
keep it short — a header block per day plus bullets. Flag anything that looks
unusual (no lessons where lessons are expected, missing teacher, a low grade,
a cancelled meal, etc.).

## Read-only

This is a read-only workflow: do not order/cancel meals, send messages, or
switch accounts as part of generating the report. If the user wants to act on
something in the report (order a meal, message a teacher), call the
corresponding write tool separately and confirm first.
"""Poll every read-only MCP tool against the live parent session and require a
well-formed, serializable result.

- Tools return MCP-shaped dicts via _run; an exception escaping `go()` is a real
  defect and always fails the test.
- `isError: true` is allowed ONLY when the tool's manifest declares a matching
  `known_error` substring (pre-existing upstream/wrapper bugs, checked here so
  the suite fails if the error changes). Everything else must succeed.
- No tool is invoked with write-producing arguments; the readonly guard in
  conftest.py makes any server-side write method explode.
"""

import datetime as dt
import json
import time

import pytest

import edupage_mcp as m

pytestmark = pytest.mark.e2e

PRIMARY = "zssturovamalacky"


def _args(cfg, sub):
    return {k: (sub if v == "@sub" else v) for k, v in cfg.items() if k != "known_error"}


def _next_weekday(today=None):
    d = today or dt.date.today()
    while d.weekday() >= 5:  # skip Sat/Sun
        d += dt.timedelta(days=1)
    return d


TOOLS = {
    "auth_status": {},
    "user_id": {"subdomain": "@sub"},
    "school_year": {"subdomain": "@sub"},
    "get_my_timetable": {"subdomain": "@sub", "date_str": _next_weekday().isoformat()},
    "get_timetable": {"target_type": "student", "target_id": "569595", "subdomain": "@sub"},
    "get_timetable_range": {
        "target_type": "student", "target_id": "569595",
        "start_date": "2026-09-07", "end_date": "2026-09-11", "subdomain": "@sub",
    },
    "get_next_ringing_time": {"subdomain": "@sub"},
    "get_next_week_timetable": {"subdomain": "@sub"},
    "get_periods": {"subdomain": "@sub"},
    # Upstream edupage-api 0.12.5 defect: get_grades() raises UnboundLocalError
    # on 'percent' when a grade entry has a grade_type outside 1/2/3 (the
    # except branch never assigns percent). Tracked, checked so the suite fails
    # if upstream fixes or changes this.
    "get_grades": {"subdomain": "@sub", "known_error": "cannot access local variable 'percent'"},
    "get_notifications": {"subdomain": "@sub"},
    "get_notification_history": {
        "date_from": (dt.date.today() - dt.timedelta(days=14)).isoformat(),
        "subdomain": "@sub",
    },
    "get_homework": {"subdomain": "@sub"},
    "get_assignments": {"subdomain": "@sub"},
    "get_absences": {"subdomain": "@sub"},
    "get_upcoming_events": {"subdomain": "@sub"},
    "get_news": {"subdomain": "@sub"},
    "get_timetable_changes": {"subdomain": "@sub"},
    "get_missing_teachers": {"subdomain": "@sub"},
    "get_meals": {"date_str": _next_weekday().isoformat(), "subdomain": "@sub"},
    "get_day_summary": {"subdomain": "@sub"},
    "get_students": {"subdomain": "@sub"},
    "get_all_students": {"subdomain": "@sub"},
    "get_teachers": {"subdomain": "@sub"},
    "get_classes": {"subdomain": "@sub"},
    "get_classrooms": {"subdomain": "@sub"},
    "get_subjects": {"subdomain": "@sub"},
    "get_my_students": {"subdomain": "@sub"},
    # Deliberately probe tiered matching with a non-existent name; expects the
    # documented graceful "no student" error. No real names are committed.
    "find_student": {"name": "ZZTestNoSuchStudentZZ", "subdomain": "@sub",
                     "known_error": "No student named"},
    "get_schools": {},
    "scan_students": {},
    # Pre-existing defect in the parent->student timetable path (upstream
    # IndexError after switch_to_child + wrapper skeleton fallback). Tracked so
    # the suite fails if the error message CHANGES, alerting us to fix it or
    # that upstream changed behaviour.
    "get_student_timetable": {
        "student_id": "569595", "subdomain": "@sub",
        "known_error": "cannot unpack non-iterable NoneType object",
    },
}


def _err_text(result):
    content = result.get("content") or []
    return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))


def _report():
    from conftest import REPORT, in_ci

    return REPORT["tools"]


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_readonly_tool_responds(tool, sessions):
    from conftest import in_ci

    cfg = TOOLS[tool]
    fn = getattr(m, tool, None)
    assert fn is not None, f"tool {tool} not found on module"
    known = cfg.get("known_error")
    args = _args(cfg, PRIMARY)
    ci = in_ci()
    t0 = time.time()
    try:
        result = fn(**args)
    except Exception as e:  # noqa: BLE001
        if ci:  # zero-data report entry: exception type only, no message/payload
            _report()[tool] = {"ok": False, "raised_type": type(e).__name__, "seconds": time.time() - t0}
        else:
            _report()[tool] = {"ok": False, "raised": f"{type(e).__name__}: {e}", "seconds": time.time() - t0}
        pytest.fail(f"{tool} raised outside _run: {type(e).__name__}: {e}")
    seconds = time.time() - t0
    assert isinstance(result, dict), f"{tool} returned non-dict {type(result)}"
    # Must be JSON-serializable (MCP transports are JSON).
    preview = json.dumps(result, ensure_ascii=False, default=str)
    if result.get("isError"):
        msg = _err_text(result)
        if known and known in msg:
            entry = {"ok": True, "known_error": msg[:160] if not ci else True, "seconds": seconds}
            _report()[tool] = entry
            return
        entry = {"ok": False, "seconds": seconds}
        if ci:
            entry["error_type"] = msg.split(":", 1)[0]
        else:
            entry["error"] = msg[:300]
        _report()[tool] = entry
        pytest.fail(f"{tool} errored: {msg[:300]}")
    entry = {"ok": True, "seconds": seconds}
    if not ci:
        entry["preview"] = preview[:120]
    _report()[tool] = entry
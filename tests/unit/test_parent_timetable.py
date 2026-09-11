"""Offline regression tests for the parent->child timetable path.

These run with NO network. They validate the wrapper logic that previously hit
upstream defects every release:

- `switch_to_child` + `get_my_timetable` for parents raises IndexError inside
  edupage-api's `__get_date_plan`, and the failed `switch_to_parent()` left the
  shared session stuck in child mode.
- The old fallback then handed a `SimpleNamespace`/`EduStudentSkeleton` to
  `client.get_timetable()`, which keys its lookup by the *exact* Python type
  (`timetables.py: lookup.get(type(target))`) and blew up with
  `TypeError: cannot unpack non-iterable NoneType object`.

The fix resolves a concrete `EduStudent` and queries the child's timetable
directly (no session switching), and `get_day_summary` reuses that timetable so
a single call returns the full report without extra lookups.

Run with:  python -m pytest tests/unit -q
"""

import datetime as dt
from types import SimpleNamespace

import pytest

import edupage_mcp as m
from edupage_api.people import EduStudent, Gender
from edupage_api.timetables import Timetable

SUB = "testschool"
D = dt.date(2026, 9, 10)

VIKTOR = EduStudent(
    person_id=111223, name="Anna Nováková", gender=Gender.FEMALE,
    in_school_since=None, class_id=701111, number_in_class=7,
)
TAMARA = EduStudent(
    person_id=222334, name="Boris Šikovný", gender=Gender.MALE,
    in_school_since=None, class_id=-6, number_in_class=1,
)


class FakeClient:
    """Parent client that can serve a child timetable. Any attempt to use the
    broken child-switch path explodes, so a passing test proves we never call it."""

    def __init__(self, children):
        self.is_logged_in = True
        self._children = list(children)
        self.last_target_type = None
        self.calls = {"get_timetable": 0, "switch_to_child": 0,
                      "switch_to_parent": 0, "get_my_timetable": 0}

    def get_students(self):
        return list(self._children)

    def get_all_students(self):
        return []

    def get_timetable(self, target, d):
        self.calls["get_timetable"] += 1
        self.last_target_type = type(target).__name__
        lesson = SimpleNamespace(
            period=1, subject=SimpleNamespace(name="Test"),
            teachers=[], classrooms=[], groups=[],
        )
        return Timetable([lesson])

    def switch_to_child(self, child):  # pragma: no cover - must never be called
        self.calls["switch_to_child"] += 1
        return "OK"

    def switch_to_parent(self):  # pragma: no cover - must never be called
        self.calls["switch_to_parent"] += 1

    def get_my_timetable(self, d):  # pragma: no cover - must never be called
        self.calls["get_my_timetable"] += 1
        return None

    def get_timetable_changes(self, d):
        return []

    def get_missing_teachers(self, d):
        return []

    def get_notifications(self):
        return []

    def get_grades(self):
        return []

    def get_meals(self, d):
        return None

    @property
    def session(self):
        # Mock homepage HTML with both children as switchChildBtn anchors
        html = (
            '<html><body>'
            '<a class="switchChildBtn" data-sid="111223">'
            '<span class="userName">Anna Nováková, VII.B</span></a>'
            '<a class="switchChildBtn" data-sid="222334">'
            '<span class="userName">Boris Šikovný, I.ZK</span></a>'
            'ASC.req_props.parent_studentid = "111223"'
            '</body></html>'
        )
        return SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=200, text=html))

    @property
    def subdomain(self):
        return SUB


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Isolate module-global session/cache state between tests."""
    saved = dict(m._clients), dict(m._roles), dict(m._student_cache)
    m._clients.clear()
    m._roles.clear()
    m._student_cache.clear()
    m._active_subdomain = None
    yield
    m._clients, m._roles = saved[0], saved[1]
    m._student_cache = saved[2]
    m._active_subdomain = None


@pytest.fixture()
def client():
    """Registered parent client at SUB with both children visible."""
    client = FakeClient([VIKTOR, TAMARA])
    m._clients[SUB] = client
    m._roles[SUB] = "parent"
    m._active_subdomain = SUB
    return client


def _disable_public_meal_widget(monkeypatch):
    """get_meals => None and the public widget fetches stay offline."""
    monkeypatch.setattr(m, "_fetch_canteen_menu", lambda *a, **k: None)
    monkeypatch.setattr(m, "_fetch_novylistok", lambda *a, **k: None)


def test_parent_resolves_real_edustudent_without_switching(client):
    raw = SimpleNamespace(person_id=111223, name="Anna Nováková", class_id=701111)
    lessons = m._get_student_timetable(client, SUB, raw, D)

    assert len(lessons) == 1
    assert client.last_target_type == "EduStudent", "upstream get_timetable needs a concrete EduStudent"
    assert client.calls["switch_to_child"] == 0
    assert client.calls["get_my_timetable"] == 0
    assert client.calls["switch_to_parent"] == 0
    assert client.calls["get_timetable"] == 1


def test_student_timetable_at_lookup_carries_student_and_lessons(client):
    res = m._student_timetable_at(client, SUB, "Anna Nováková", None, D)
    assert res is not None
    assert res["student_id"] == 111223
    assert res["student"] == "Anna Nováková"
    assert len(res["lessons"]) == 1
    assert res["_student_obj"] is not None


def test_get_student_timetable_tool_no_longer_errors(client):
    res = m.get_student_timetable(student_id=111223, subdomain=SUB)
    assert not res.get("isError")
    results = res.get("results") or []
    assert len(results) == 1
    assert len(results[0].get("lessons") or []) == 1
    # The defect we fixed surfaced exactly this error; project it stays gone.
    assert "cannot unpack" not in str(res)


def test_get_day_summary_single_call_all_sections(client, monkeypatch):
    _disable_public_meal_widget(monkeypatch)
    res = m.get_day_summary(name="Anna Nováková", date_str="2026-09-10", subdomain=SUB)
    assert not res.get("isError")
    results = res.get("results") or []
    assert len(results) == 1
    r0 = results[0]
    assert r0["student"]["name"] == "Anna Nováková"
    sections = r0.get("sections", {})
    # Single call must deliver the whole report, timetable included.
    expected = {
        "timetable", "substitutions", "missing_teachers", "grades", "meals",
        "homework", "assignments", "absences", "news", "events", "notifications",
    }
    assert expected <= set(sections)
    assert all(v.get("ok") for v in sections.values()), sections
    assert len(sections["timetable"].get("lessons") or []) == 1
    # The timetable from discovery must be reused, not re-fetched per section.
    assert client.calls["get_timetable"] == 1


def test_get_day_summary_auto_discovers_every_child(client, monkeypatch):
    """Default (parent, no student named) returns a lightweight per-school
    discovery index — NOT full per-child reports. Keeps the payload small and
    avoids mixing children across schools / timeouts with many children."""
    _disable_public_meal_widget(monkeypatch)
    res = m.get_day_summary(date_str="2026-09-10", subdomain=SUB)
    assert not res.get("isError")
    assert res.get("mode") == "discovery"
    results = res.get("results") or []
    assert len(results) == 1, results
    students = results[0].get("students") or []
    names = {s["name"] for s in students}
    assert {"Anna Nováková", "Boris Šikovný"} == names
    for s in students:
        assert s["student_id"] is not None
    assert all("sections" not in r for r in results), "discovery must not fetch full sections"
    # No per-child timetable fetches happen during a pure discovery call.
    assert client.calls["get_timetable"] == 0


def test_get_day_summary_full_builds_every_child_report(client, monkeypatch):
    """full=True keeps the old all-children behavior: one full report per child."""
    _disable_public_meal_widget(monkeypatch)
    res = m.get_day_summary(date_str="2026-09-10", subdomain=SUB, full=True)
    assert not res.get("isError")
    children = res.get("results") or []
    names = {c["student"]["name"] for c in children}
    assert {"Anna Nováková", "Boris Šikovný"} == names
    for child in children:
        assert child["sections"]["timetable"].get("ok") is True


def test_discovery_subdomains_limited_to_env(monkeypatch):
    """When EDUPAGE_SUBDOMAINS is set, discovery never touches unconfigured schools."""
    monkeypatch.setattr(m, "EDUPAGE_SUBDOMAINS", "schoola,schoolb")
    m._clients["unconfigured-school"] = None
    assert m._discovery_subdomains() == ["schoola", "schoolb"]


def test_discovery_subdomains_falls_back_to_all_clients(monkeypatch):
    """When EDUPAGE_SUBDOMAINS is unset, discovery scans every logged-in client."""
    monkeypatch.setattr(m, "EDUPAGE_SUBDOMAINS", "")
    m._clients["schoola"] = object()
    m._clients["schoolb"] = object()
    m._clients[None] = None
    assert m._discovery_subdomains() == ["schoola", "schoolb"]


def test_get_day_summary_scans_only_configured_subdomains(client, monkeypatch):
    """No-name discovery report only covers EDUPAGE_SUBDOMAINS schools, even when
    extra sessions exist. Prevents the cross-school mixing from previous runs."""
    _disable_public_meal_widget(monkeypatch)
    monkeypatch.setattr(m, "EDUPAGE_SUBDOMAINS", SUB)
    other = FakeClient([VIKTOR])
    m._clients["unconfigured-school"] = other
    m._roles["unconfigured-school"] = "parent"
    res = m.get_day_summary(date_str="2026-09-10")
    assert not res.get("isError")
    assert res.get("mode") == "discovery"
    results = res.get("results") or []
    assert [s["subdomain"] for s in results] == [SUB]
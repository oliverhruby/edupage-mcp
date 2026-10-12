"""Offline regression tests for upstream ``MissingDataException`` handling.

edupage-api 0.13.2 (PR #129) replaced the bare ``IndexError`` that
``Timetables.__get_date_plan`` used to raise with ``MissingDataException``.
That class is a plain ``Exception`` -- it does **not** subclass
``RequestError`` (``edupage_api/exceptions.py``) -- so the four timetable
handlers that previously degraded to an empty result would have started
returning ``{"isError": ...}`` instead.

The most likely trigger is a parent account with no child selected:
``timetables.py`` raises ``MissingDataException("A parent account has no
timetable of its own, select a child with switch_to_child() first.")``.

These run with NO network. Run with:  python -m pytest tests/unit -q
"""

import datetime as dt
from types import SimpleNamespace

import pytest

import edupage_mcp as m
from edupage_api import exceptions as edupage_exceptions
from edupage_api.people import EduStudent, Gender

SUB = "testschool"
D = dt.date(2026, 9, 10)

PARENT_MSG = ("A parent account has no timetable of its own, "
              "select a child with switch_to_child() first.")

STUDENT = EduStudent(
    person_id=111223, name="Test Student", gender=Gender.MALE,
    in_school_since=None, class_id=701111, number_in_class=7,
)


class RaisingClient:
    """Client whose timetable calls raise MissingDataException, exactly as
    Timetables does for a parent account with no child selected.

    ``timetable_raises`` controls only ``get_timetable(target, d)``:
    ``get_my_timetable`` always raises. ``_get_student_timetable`` needs the
    by-object lookup to succeed so it can be shown to avoid switching; the
    ``_target_timetable_day`` test needs it to raise.
    """

    def __init__(self, timetable_raises=True):
        self.is_logged_in = True
        self.subdomain = SUB
        self.timetable_raises = timetable_raises
        self.calls = {"switch_to_child": 0, "switch_to_parent": 0}

    def switch_to_child(self, *a, **k):
        self.calls["switch_to_child"] += 1

    def switch_to_parent(self, *a, **k):
        self.calls["switch_to_parent"] += 1

    def get_timetable(self, target, d):
        if self.timetable_raises:
            raise edupage_exceptions.MissingDataException(
                "The server returned an incorrect response.")
        return SimpleNamespace(lessons=[])

    def get_my_timetable(self, d):
        raise edupage_exceptions.MissingDataException(PARENT_MSG)

    def get_user_id(self):
        return "Rodic"

    def get_students(self):
        return [STUDENT]

    def get_all_students(self):
        return []


@pytest.fixture
def client(monkeypatch):
    saved_clients, saved_roles = dict(m._clients), dict(m._roles)
    saved_active = m._active_subdomain
    m._clients.clear()
    m._roles.clear()
    m._active_subdomain = None
    monkeypatch.setattr(m, "EDUPAGE_SUBDOMAINS", SUB)
    c = RaisingClient()
    m._clients[SUB] = c
    m._roles[SUB] = "parent"
    m._active_subdomain = SUB
    yield c
    m._clients.clear()
    m._clients.update(saved_clients)
    m._roles.clear()
    m._roles.update(saved_roles)
    m._active_subdomain = saved_active


def test_missing_data_is_not_a_request_error():
    """Why the old tuple could not have caught it: it never subclassed the
    exception type any of our handlers were written around."""
    assert not issubclass(edupage_exceptions.MissingDataException,
                          edupage_exceptions.RequestError)


def test_get_my_timetable_degrades_to_empty_lessons(client):
    result = m.get_my_timetable(date_str="2026-09-10")
    assert not result.get("isError"), result
    assert result["lessons"] == []


def test_get_next_week_timetable_degrades_to_empty_lessons(client):
    result = m.get_next_week_timetable()
    assert not result.get("isError"), result


def test_my_timetable_lessons_degrades_to_empty_list(client):
    assert m._my_timetable_lessons(client, D) == []


def test_target_timetable_day_degrades_to_empty_lessons(client):
    result = m._target_timetable_day(client, "student", "111223", D, SUB)
    assert not result.get("isError"), result
    assert result["lessons"] == []


def test_parent_timetable_prefers_the_by_object_path_over_switching(client):
    """The comment above _get_student_timetable says the parent path prefers a
    direct get_timetable(real, d) call over switching sessions. Keep that true:
    when the by-object lookup succeeds, no session switch happens."""
    client.timetable_raises = False
    assert m._get_student_timetable(client, SUB, STUDENT, D) == []
    assert client.calls["switch_to_child"] == 0
    assert client.calls["switch_to_parent"] == 0


def test_parent_timetable_falls_back_to_switching_when_lookup_raises(client):
    """...and when it raises, the guarded last-resort switch path runs and
    still restores the session, even though get_my_timetable keeps raising."""
    assert m._get_student_timetable(client, SUB, STUDENT, D) == []
    assert client.calls["switch_to_child"] == 1
    assert client.calls["switch_to_parent"] == 1
"""Homework/exam day-filtering must match on the DUE date, not the publish date.

Regression tests for the "homework for tomorrow" defect: EduPage carries two
distinct dates per timeline entry -- `timestamp` (when the entry was published,
i.e. when the teacher assigned it) and `event_time` (the deadline, EduPage's
`cas_udalosti`). `_timeline_on_day` used to match only on `timestamp`, so
homework assigned days in advance for a given day was invisible to every query
except the one for its assignment date.

These run with NO network and NO credentials: the timeline events are built
here, shaped like `edupage_api.timeline.TimelineEvent`.
"""

import datetime
from types import SimpleNamespace

import pytest

import edupage_mcp as m

HOMEWORK = m.EventType.HOMEWORK
HOMEWORK_STATE = m.EventType.HOMEWORK_STUDENT_STATE
NEWS = m.EventType.NEWS


def _event(event_type, published, deadline=None, text="x"):
    return SimpleNamespace(
        event_id="e",
        timestamp=datetime.datetime.combine(published, datetime.time(8, 0)),
        event_time=(
            datetime.datetime.combine(deadline, datetime.time(23, 59))
            if deadline is not None
            else None
        ),
        text=text,
        event_type=event_type,
        additional_data={},
    )


class _Client:
    def __init__(self, events):
        self._events = events

    def get_notifications(self):
        return list(self._events)


def _by_day(client, day, types, **kw):
    return m._timeline_on_day(client, day, types, **kw)


ASSIGNED_MONDAY = datetime.date(2026, 9, 21)   # a Monday
DUE_TUESDAY = datetime.date(2026, 9, 29)
DUE_MONDAY_LATER = datetime.date(2026, 10, 5)
SAME_DAY = datetime.date(2026, 9, 28)


def test_homework_due_on_day_is_found_even_if_assigned_earlier():
    """The core defect: assigned the 21st, due the 29th -> asking for the 29th
    used to return nothing."""
    client = _Client([_event(HOMEWORK, ASSIGNED_MONDAY, DUE_TUESDAY)])

    on_publish_day = _by_day(client, ASSIGNED_MONDAY, m._HOMEWORK_TYPES, prefer_deadline=True)
    on_due_day = _by_day(client, DUE_TUESDAY, m._HOMEWORK_TYPES, prefer_deadline=True)

    assert on_due_day, "homework due on a day must appear on that day"
    assert not on_publish_day, (
        "homework assigned on a day should not be reported as due on that day"
    )
    # The deadline must survive serialization so the model can see it.
    assert on_due_day[0]["event_time"].startswith(DUE_TUESDAY.isoformat())


def test_publish_date_filter_is_the_previous_broken_behaviour():
    """Pins the exact regression: without prefer_deadline the item lands on the
    assignment date, which is what get_day_summary used to do."""
    client = _Client([_event(HOMEWORK, ASSIGNED_MONDAY, DUE_TUESDAY)])

    assert _by_day(client, ASSIGNED_MONDAY, m._HOMEWORK_TYPES)
    assert not _by_day(client, DUE_TUESDAY, m._HOMEWORK_TYPES)


def test_multiple_deadlines_are_grouped_by_due_day():
    events = [
        _event(HOMEWORK, datetime.date(2026, 9, 21), DUE_TUESDAY, "a"),
        _event(HOMEWORK, datetime.date(2026, 9, 22), DUE_TUESDAY, "b"),
        _event(HOMEWORK, datetime.date(2026, 9, 23), DUE_MONDAY_LATER, "c"),
        _event(HOMEWORK_STATE, datetime.date(2026, 9, 24), DUE_MONDAY_LATER, "d"),
    ]
    client = _Client(events)

    tuesday = _by_day(client, DUE_TUESDAY, m._HOMEWORK_TYPES, prefer_deadline=True)
    later = _by_day(client, DUE_MONDAY_LATER, m._HOMEWORK_TYPES, prefer_deadline=True)

    assert len(tuesday) == 2
    assert len(later) == 2, "both HOMEWORK and HOMEWORK_STUDENT_STATE must match"


def test_homework_due_today_assigned_today_still_works():
    today = datetime.date(2026, 9, 30)
    client = _Client([_event(HOMEWORK, today, today)])

    assert len(_by_day(client, today, m._HOMEWORK_TYPES, prefer_deadline=True)) == 1


def test_falls_back_to_timestamp_when_deadline_missing():
    """edupage-api < 0.12.7 has no event_time at all, and some event types carry
    no deadline. Those must still be matched on timestamp, never dropped."""
    published = datetime.date(2026, 9, 28)
    client = _Client([_event(HOMEWORK, published, None)])

    got = _by_day(client, published, m._HOMEWORK_TYPES, prefer_deadline=True)
    assert len(got) == 1, "no deadline must fall back to timestamp, not vanish"

    # An event with neither date is dropped rather than crashing.
    client = _Client([SimpleNamespace(event_type=HOMEWORK, timestamp=None, event_time=None)])
    assert _by_day(client, published, m._HOMEWORK_TYPES, prefer_deadline=True) == []


def test_non_deadline_sections_still_use_publish_date():
    """News has no meaningful due date; its section must keep matching on
    timestamp so existing behaviour is unchanged."""
    published = datetime.date(2026, 9, 28)
    client = _Client([_event(NEWS, published, datetime.date(2026, 10, 5))])

    assert _by_day(client, published, {NEWS})
    assert not _by_day(client, datetime.date(2026, 10, 5), {NEWS})


def test_type_filter_still_applies_in_deadline_mode():
    client = _Client([_event(NEWS, ASSIGNED_MONDAY, DUE_TUESDAY)])

    assert _by_day(client, DUE_TUESDAY, m._HOMEWORK_TYPES, prefer_deadline=True) == []


@pytest.mark.parametrize("day", [ASSIGNED_MONDAY, DUE_TUESDAY, DUE_MONDAY_LATER])
def test_no_day_returns_a_duplicate_of_another(day):
    """An item belongs to exactly one day, never two."""
    client = _Client([_event(HOMEWORK, ASSIGNED_MONDAY, DUE_TUESDAY)])

    got = _by_day(client, day, m._HOMEWORK_TYPES, prefer_deadline=True)
    assert len(got) <= 1

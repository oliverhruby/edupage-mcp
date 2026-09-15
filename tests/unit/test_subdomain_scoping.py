"""Offline regression tests for EDUPAGE_SUBDOMAINS scoping.

These run with NO network. The wrapper guarantees that a subdomain the operator
did not opt in to (EDUPAGE_SUBDOMAINS is set and does not contain it) is never
treated as a login candidate: no "call `login`" prompt, no implicit
re-login. Only when EDUPAGE_SUBDOMAINS is empty (auto-discovery mode) is any
subdomain treated as a legitimate target.

Run with:  python -m pytest tests/unit -q
"""

import pytest

import edupage_mcp as m

SUB = "testschool"


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Isolate module-global session/cache state between tests."""
    saved = dict(m._clients), dict(m._roles), dict(m._autologin_failures)
    m._clients.clear()
    m._roles.clear()
    m._autologin_failures.clear()
    m._active_subdomain = None
    yield
    m._clients, m._roles = saved[0], saved[1]
    m._autologin_failures = saved[2]
    m._active_subdomain = None


def _set_scope(monkeypatch, value):
    monkeypatch.setattr(m, "EDUPAGE_SUBDOMAINS", value)


class _FakeEdupage:
    """Minimal stand-in that records login attempts instead of hitting EduPage."""

    is_logged_in = True

    def __init__(self):
        self.calls = []
        self.last_login = None

    def login(self, user, pwd, subdomain):
        self.calls.append((user, pwd, subdomain))
        self.last_login = subdomain
        return None

    def get_user_id(self):
        return "Student123"


def test_unconfigured_subdomain_is_never_a_login_prompt(monkeypatch):
    _set_scope(monkeypatch, "schoola, schoolb")
    msg = m._login_block_message("notinlist")
    assert msg is not None
    assert "not configured in EDUPAGE_SUBDOMAINS" in msg
    assert "Call `login`" not in msg


def test_configured_subdomain_still_prompts_login(monkeypatch):
    _set_scope(monkeypatch, "schoola,schoolb")
    msg = m._login_block_message("schoola")
    assert msg is not None
    assert "Call `login`" in msg


def test_empty_scope_keeps_login_prompt(monkeypatch):
    _set_scope(monkeypatch, "")
    msg = m._login_block_message("schoola")
    assert msg is not None
    assert "Call `login`" in msg


def test_relogin_refuses_unconfigured_subdomain(monkeypatch):
    _set_scope(monkeypatch, "schoola")
    monkeypatch.setattr(m, "EDUPAGE_USERNAME", "user")
    monkeypatch.setattr(m, "EDUPAGE_PASSWORD", "pass")
    fake = _FakeEdupage()
    monkeypatch.setattr(m, "Edupage", lambda: fake)

    assert m._relogin_subdomain("outofscope") is False
    assert fake.calls == []
    assert "outofscope" not in m._clients


def test_relogin_attempts_configured_subdomain(monkeypatch):
    _set_scope(monkeypatch, "schoola")
    monkeypatch.setattr(m, "EDUPAGE_USERNAME", "user")
    monkeypatch.setattr(m, "EDUPAGE_PASSWORD", "pass")
    fake = _FakeEdupage()
    monkeypatch.setattr(m, "Edupage", lambda: fake)

    assert m._relogin_subdomain("schoola") is True
    assert fake.calls == [("user", "pass", "schoola")]
    assert m._clients["schoola"] is fake


def test_relogin_records_scope_failure_for_unconfigured(monkeypatch):
    _set_scope(monkeypatch, "schoola")
    monkeypatch.setattr(m, "EDUPAGE_USERNAME", "user")
    monkeypatch.setattr(m, "EDUPAGE_PASSWORD", "pass")
    fake = _FakeEdupage()
    monkeypatch.setattr(m, "Edupage", lambda: fake)

    assert m._relogin_subdomain("outofscope") is False
    assert "not in EDUPAGE_SUBDOMAINS scope" in m._autologin_failures["outofscope"]
    assert "outofscope" not in m._clients
    assert fake.last_login is None


def test_discovery_helpers_agree_with_scope(monkeypatch):
    _set_scope(monkeypatch, "a, b ,  c")
    assert m._configured_subdomains() == ["a", "b", "c"]
    assert m._subdomain_is_configured("a") is True
    assert m._subdomain_is_configured("zzz") is False
    _set_scope(monkeypatch, "")
    assert m._configured_subdomains() == []
    assert m._subdomain_is_configured("zzz") is True
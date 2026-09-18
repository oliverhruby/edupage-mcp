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
    saved = (dict(m._clients), dict(m._roles), dict(m._autologin_failures),
             dict(m._two_factor), dict(m._student_cache))
    m._clients.clear()
    m._roles.clear()
    m._autologin_failures.clear()
    m._two_factor.clear()
    m._student_cache.clear()
    m._active_subdomain = None
    yield
    m._clients, m._roles = saved[0], saved[1]
    m._autologin_failures = saved[2]
    m._two_factor = saved[3]
    m._student_cache = saved[4]
    m._active_subdomain = None


def _set_scope(monkeypatch, value):
    monkeypatch.setattr(m, "EDUPAGE_SUBDOMAINS", value)


class _FakeEdupage:
    """Minimal stand-in that records login attempts instead of hitting EduPage."""

    is_logged_in = True
    subdomain = None

    def __init__(self):
        self.calls = []
        self.last_login = None
        self.discovered_subdomains = []

    def login(self, user, pwd, subdomain):
        self.calls.append((user, pwd, subdomain))
        self.last_login = subdomain
        return None

    def login_auto(self, user, pwd):
        self.calls.append((user, pwd, "auto"))
        self.last_login = "auto"
        return None

    def get_subdomains(self):
        return list(self.discovered_subdomains)

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


# -- Regression: with EDUPAGE_SUBDOMAINS set it is a strict allowlist. An
# out-of-scope school must be unusable and invisible even when a stale
# client/session happens to exist in `_clients` (e.g. left from an earlier
# config, a manual login before the scope changed, or a server restart that
# kept old sessions).


def test_live_out_of_scope_client_is_still_blocked(monkeypatch):
    """A live session for an unconfigured school must still be refused."""
    _set_scope(monkeypatch, "schoola")
    fake = _FakeEdupage()
    m._clients["schoolb"] = fake
    msg = m._login_block_message("schoolb")
    assert msg is not None
    assert "not configured in EDUPAGE_SUBDOMAINS" in msg
    assert "Call `login`" not in msg
    with pytest.raises(RuntimeError):
        m._require_client("schoolb")


def test_get_subdomains_never_lists_or_reports_out_of_scope(monkeypatch):
    """get_subdomains hides stale out-of-scope sessions and live-discovered
    schools beyond the allowlist from every output section."""
    _set_scope(monkeypatch, "schoola")
    fake = _FakeEdupage()
    fake.discovered_subdomains = ["schoola", "schoolb"]  # portal claims schoolb too
    m._clients["schoola"] = fake
    m._clients["schoolb"] = fake  # stale out-of-scope session
    m._roles["schoola"] = "parent"
    monkeypatch.setattr(m, "_require_client", lambda *a, **k: fake)

    out = m.get_subdomains()
    assert out["subdomains"] == ["schoola"]
    school_subs = [s["subdomain"] for s in out["schools"]]
    assert school_subs == ["schoola"]


def test_get_subdomains_hides_out_of_scope_failures(monkeypatch):
    """failed_logins never surfaces entries for unconfigured schools."""
    _set_scope(monkeypatch, "schoola")
    m._autologin_failures["schoolb"] = "boom"
    m._autologin_failures["schoola"] = "2FA required"
    out = m.get_subdomains()
    assert "schoolb" not in out["failed_logins"]
    assert out["failed_logins"] == {"schoola": "2FA required"}


def test_login_refuses_out_of_scope_subdomain(monkeypatch):
    _set_scope(monkeypatch, "schoola")
    monkeypatch.setattr(m, "EDUPAGE_USERNAME", "user")
    monkeypatch.setattr(m, "EDUPAGE_PASSWORD", "pass")
    fake = _FakeEdupage()
    monkeypatch.setattr(m, "Edupage", lambda: fake)

    out = m.login(username="user", password="pass", subdomain="schoolb")
    assert out.get("isError") is True
    assert "not configured in EDUPAGE_SUBDOMAINS" in out["content"][0]["text"]
    assert fake.calls == []
    assert "schoolb" not in m._clients


def test_login_all_refuses_out_of_scope_per_entry(monkeypatch):
    _set_scope(monkeypatch, "schoola")
    fake = _FakeEdupage()
    monkeypatch.setattr(m, "Edupage", lambda: fake)

    out = m.login_all(subdomains="schoolb,schoola", usernames="user", passwords="pass")
    results = {r["subdomain"]: r for r in out["results"]}
    assert results["schoolb"]["ok"] is False
    assert "not configured in EDUPAGE_SUBDOMAINS" in results["schoolb"]["error"]
    assert results["schoola"]["ok"] is True
    assert fake.calls == [("user", "pass", "schoola")]
    assert "schoolb" not in m._clients


def test_login_auto_refuses_out_of_scope_discovered_school(monkeypatch):
    _set_scope(monkeypatch, "schoola")
    fake = _FakeEdupage()
    fake.subdomain = "schoolb"
    monkeypatch.setattr(m, "Edupage", lambda: fake)

    out = m.login(method="auto", username="user", password="pass")
    assert out.get("isError") is True
    assert "not configured in EDUPAGE_SUBDOMAINS" in out["content"][0]["text"]
    assert "schoolb" not in m._clients
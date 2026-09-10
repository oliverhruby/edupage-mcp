"""Shared fixtures for the live integration suite.

SECURITY: this suite hits the real EduPage API. Locally it may store real
student data in reports/ for drift review. On a public CI runner it MUST be run
with EDUPAGE_E2E_CI=1 so the report stays zero-data (see `in_ci`) and no
artifacts are uploaded; child identities are then verified only against the
one-way fingerprints in tests/e2e/expected_children.fingerprint.json.

Enable with EDUPAGE_E2E=1 plus EDUPAGE_USERNAME, EDUPAGE_PASSWORD,
EDUPAGE_SUBDOMAINS. Without them every test skips (never fails, never logs
anything).
"""

import json
import os
import time

import pytest

import edupage_mcp as m

# Schools this suite is allowed to touch. The user's test scope is restricted
# to these two; never include unreviewed subdomains (e.g. cvcmalacky).
ALLOWED_SUBDOMAINS = ("zssturovamalacky", "iprskola")

# edupage-api methods that WRITE data server-side. The suite monkeypatches
# these to raise, so any accidental write attempt fails the run loudly.
WRITE_METHODS = (
    "send_message",
    "choose",
    "rate",
    "sign_off",
    "sign_into_lesson",
    "resend_notifications",
    "upload_file",
    "cloud_upload",
)

# Current run report; written to disk at session teardown.
REPORT = {"session_started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tools": {}}


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: live integration against a real EduPage server (local-only)")


def _enabled():
    if os.environ.get("EDUPAGE_E2E") != "1":
        return False
    return all(
        os.environ.get(k) for k in ("EDUPAGE_USERNAME", "EDUPAGE_PASSWORD", "EDUPAGE_SUBDOMAINS")
    )


def in_ci():
    """True on a public CI runner (EDUPAGE_E2E_CI=1): report entries must then
    carry NO personal data (no payload previews, no child ids, no identity
    text) — zero-data output by design, not by reliance on log masking."""
    return os.environ.get("EDUPAGE_E2E_CI") == "1"


pytestmark = pytest.mark.skipif(
    not _enabled(),
    reason=(
        "Live e2e disabled. Set EDUPAGE_E2E=1 and EDUPAGE_USERNAME/PASSWORD/SUBDOMAINS. "
        "Locally it may store real student data; public CI must set EDUPAGE_E2E_CI=1.",
    ),
)


@pytest.fixture(scope="session")
def subdomains():
    """The exact subdomains this suite may log into and test against."""
    raw = os.environ.get("EDUPAGE_SUBDOMAINS", "")
    subs = [s.strip() for s in raw.split(",") if s.strip()]
    missing = [s for s in ALLOWED_SUBDOMAINS if s not in subs]
    extra = [s for s in subs if s not in ALLOWED_SUBDOMAINS]
    assert not missing, (
        f"EDUPAGE_SUBDOMAINS must include {ALLOWED_SUBDOMAINS}; missing {missing}. "
        "The live suite is deliberately restricted to these schools."
    )
    assert not extra, (
        f"EDUPAGE_SUBDOMAINS contains unapproved schools {extra}. "
        "Only {ALLOWED_SUBDOMAINS} may be exercised by this suite."
    )
    return tuple(subs)


@pytest.fixture(scope="session")
def sessions(subdomains):
    """Log in to every approved school via the module's real login path and
    assert the account resolves to a parent role with no 2FA pending."""
    m._clients.clear()
    m._two_factor.clear()
    m._roles.clear()
    m._student_cache.clear()
    m._active_subdomain = None
    m._autologin_failures = {}

    result = {}
    for sub in subdomains:
        res = m.login(  # noqa: SLF001 - calling our own singleton fixture path
            subdomain=sub,
            username=os.environ["EDUPAGE_USERNAME"],
            password=os.environ["EDUPAGE_PASSWORD"],
        )
        assert isinstance(res, dict) and not res.get("isError"), f"login failed for {sub}: {res}"
        assert res.get("logged_in") is True, f"not logged in for {sub}: {res}"
        assert res.get("two_factor_required") is False, (
            f"2FA pending for {sub}. Complete device confirmation for this login "
            "before running the suite (EduPage confirms new locations/IPs)."
        )
        role = m._roles.get(sub)
        assert role == "parent", f"expected parent role at {sub}, got {role!r}"
        client = result[sub] = m._clients[sub]
        assert client.is_logged_in, f"client not logged in for {sub}"
    return result


@pytest.fixture(autouse=True, scope="session")
def _readonly_guard():
    """Make every server-side write method explode if called during the suite."""
    import edupage_api

    saved = {}
    for name in WRITE_METHODS:
        if hasattr(edupage_api.Edupage, name):
            saved[name] = getattr(edupage_api.Edupage, name)

            def _boom(self, *args, _name=name, **kwargs):  # noqa: ANN001
                raise AssertionError(
                    f"WRITE DETECTED: edupage-api.Edupage.{_name} was invoked. "
                    "The live integration suite must be read-only."
                )

            setattr(edupage_api.Edupage, name, _boom)
    yield
    for name, fn in saved.items():
        setattr(edupage_api.Edupage, name, fn)


def pytest_sessionfinish(session, exitstatus):
    """Write the run report locally. Contains real data — stays on the machine."""
    if not _enabled():
        return
    REPORT["session_finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    report_dir = os.environ.get("E2E_REPORT_DIR", "reports")
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "e2e-report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(REPORT, f, indent=2, ensure_ascii=False, default=str)
    session.config.option.verbose and print(f"\ne2e report written to {path}")
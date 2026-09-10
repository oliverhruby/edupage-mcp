"""Site-parsing contract: the EduPage homepage markup our wrapper depends on
(`ASC.req_props.parent_studentid` and the `.switchChildBtn` child-switch
anchors) must still be present and parse to the parent's children.

This is the tripwire for 'did EduPage change the website code we're parsing?'.
NO real student identities are hard-coded here. Exact expected children are
optional and live in the gitignored `tests/e2e/.local.e2e.json` (only ever
present on the owner's machine):
    {"children": {"zssturovamalacky": {"569595": "Viktor Hruby"}, ...}}
Without that file, only structural + self-consistency checks run.
"""

import json
import os
import time

import pytest

import edupage_mcp as m

pytestmark = pytest.mark.e2e

REQUIRED_MARKERS = ("parent_studentid", "switchChildBtn")
_LOCAL_EXPECTED = os.path.join(os.path.dirname(__file__), ".local.e2e.json")


def _expected_children():
    if not os.path.exists(_LOCAL_EXPECTED):
        return None
    data = json.load(open(_LOCAL_EXPECTED, encoding="utf-8"))
    return {
        sub: {int(pid): name for pid, name in subs.items()}
        for sub, subs in data.get("children", {}).items()
    }


@pytest.mark.parametrize("sub", ["zssturovamalacky", "iprskola"])
def test_homepage_markers_present(sub, sessions):
    client = sessions[sub]
    resp = client.session.get(f"https://{sub}.edupage.org/")
    assert resp.status_code == 200, f"homepage HTTP {resp.status_code} for {sub}"
    page = resp.text
    for marker in REQUIRED_MARKERS:
        assert marker in page, f"homepage marker '{marker}' missing at {sub} — EduPage layout change?"
    conftest_report()["homepage_marker_check_ok"][sub] = True


@pytest.mark.parametrize("sub", ["zssturovamalacky", "iprskola"])
def test_parent_children_parse(sub, sessions):
    from conftest import in_ci

    ci = in_ci()
    client = sessions[sub]
    children = m._get_parent_children(client, sub)
    assert children, _m(ci, f"no children parsed at {sub} — switchChildBtn/parent_studentid broken?",
                        f"no children parsed at {sub}")
    ids = set()
    for c in children:
        assert isinstance(c.person_id, int), _m(ci, f"non-int person_id {c.person_id!r} at {sub}",
                                                f"non-int person_id at {sub}")
        ids.add(c.person_id)
        assert c.name and " " in c.name, _m(ci, f"name {c.name!r} at {sub} is not a full name",
                                            f"name malformed at {sub}")
        assert not c.name.rstrip().endswith(","), _m(ci, f"class suffix not stripped: {c.name!r} at {sub}",
                                                     f"class suffix not stripped at {sub}")
    if not ci:  # child ids are personal data — never recorded in public CI runs
        conftest_report()["children"][sub] = sorted(ids)


def test_children_self_consistent_with_tool(sessions, subdomains):
    """get_my_students() and scan_students() must agree with the parsed children."""
    from conftest import in_ci

    ci = in_ci()
    parsed_by_sub = {}
    for sub in subdomains:
        client = sessions[sub]
        parsed_by_sub[sub] = {c.person_id for c in m._get_parent_children(client, sub)}

    for sub in subdomains:
        res = m.get_my_students(subdomain=sub)
        assert isinstance(res, dict) and not res.get("isError"), _m(
            ci, f"get_my_students error: {res}", f"get_my_students error at {sub}")
        tool_ids = {s["person_id"] for s in res.get("students", [])}
        assert tool_ids == parsed_by_sub[sub], _m(
            ci,
            f"get_my_students({sub})={tool_ids} != parsed children {parsed_by_sub[sub]}",
            f"get_my_students({sub}) does not match parsed children",
        )

    scan = m.scan_students()
    assert isinstance(scan, dict) and not scan.get("isError"), _m(
        ci, f"scan_students error: {scan}", "scan_students error")
    discovered = scan.get("students")
    assert isinstance(discovered, list), "scan_students missing 'students' key"
    merged = {sub: set() for sub in subdomains}
    for entry in discovered:
        assert entry["subdomain"] in subdomains, f"scan hit unapproved school {entry['subdomain']}"
        merged[entry["subdomain"]].add(entry["student_id"])
        assert entry["name"] and entry["student_id"] is not None, _m(
            ci, f"scan entry malformed: {entry!r}", "scan entry malformed")
    for sub in subdomains:
        assert merged[sub] == parsed_by_sub[sub], _m(
            ci,
            f"scan_students({sub})={merged[sub]} != parsed children {parsed_by_sub[sub]}",
            f"scan_students({sub}) does not match parsed children",
        )


def test_expected_children_fingerprint_matches(sessions, subdomains):
    """Parsed children must match the committed one-way fingerprints. Runs
    everywhere (incl. public CI) and reveals NO personal data — a mismatch is
    reported as hash values only."""
    from fingerprint import children_fingerprint, expected_for

    for sub in subdomains:
        exp = expected_for(sub)
        assert exp, (
            f"no expected fingerprint for {sub} in expected_children.fingerprint.json. "
            "Regenerate locally with a checkout that has tests/e2e/.local.e2e.json."
        )
        got = children_fingerprint(m._get_parent_children(sessions[sub], sub))
        assert got == exp, (
            f"children at {sub} drifted from the expected fingerprint "
            f"(expected {exp}, got {got}). Either EduPage changed how children "
            "are rendered, or the account's children changed. Confirm locally "
            "with tests/e2e/.local.e2e.json, then regenerate the fingerprint."
        )


def test_expected_children_match_if_configured(sessions, subdomains):
    """Only runs on the owner's machine: asserts exact identities from the
    gitignored .local.e2e.json. Never committed, never in public CI."""
    expected = _expected_children()
    if expected is None:
        pytest.skip("tests/e2e/.local.e2e.json not present — exact identity check skipped")
    for sub in subdomains:
        children = m._get_parent_children(sessions[sub], sub)
        got = {c.person_id: c.name for c in children}
        exp = expected.get(sub, {})
        assert got == exp, (
            f"children changed at {sub}: expected {exp}, got {got}. "
            "This is either a legitimate change (update .local.e2e.json) or EduPage "
            "changed how children are rendered."
        )


def conftest_report():
    from conftest import REPORT

    REPORT.setdefault("homepage_marker_check_ok", {})
    REPORT.setdefault("children", {})
    return REPORT


def _m(ci, detail, generic):
    """Failure message: detailed locally (may contain personal values), generic
    on public CI so nothing personal ever reaches job logs."""
    return generic if ci else detail
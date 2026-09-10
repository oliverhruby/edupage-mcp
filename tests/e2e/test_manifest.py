"""Manifest completeness: every MCP tool the server registers must be accounted
for as either polled-in-e2e, write/mutating (never invoked), or auth/local-only.
Adding a new tool (or deleting one) must update this manifest — the test fails
loudly otherwise, so no write-capable tool silently falls into e2e polling."""

import asyncio

import pytest

import edupage_mcp as m
from test_readonly_tools import TOOLS

pytestmark = pytest.mark.e2e

# Names of tools that WRITE server-side state (or allow arbitrary requests) and
# must NEVER be invoked by the suite.
WRITE_TOOLS = {
    "send_message",
    "switch_to_student",
    "switch_to_parent",
    "choose_meal",
    "sign_off_meal",
    "rate_meal",
    "custom_request",
}

# Auth / local-only tools that are intentionally not polled.
AUTH_OR_LOCAL = {
    "login",
    "login_all",
    "login_auto",
    "login_from_session",
    "two_factor_check_confirmed",
    "two_factor_finish",
    "clear_student_cache",
}


def _registered():
    if m.server is None:
        pytest.skip("fastmcp unavailable (mcp package not installed)")
    tools = asyncio.run(m.server.list_tools())
    return {t.name for t in tools}


def test_every_tool_accounted_for():
    registered = _registered()
    accounted = set(TOOLS) | WRITE_TOOLS | AUTH_OR_LOCAL
    unaccounted = registered - accounted
    assert not unaccounted, (
        f"tools not in the e2e manifest: {sorted(unaccounted)}. "
        "Classify them as polled, write, or auth/local in tests/e2e/test_manifest.py."
    )
    stale = accounted - registered
    assert not stale, f"manifest references tools that no longer exist: {sorted(stale)}"


def test_no_write_tool_polled():
    assert not (set(TOOLS) & WRITE_TOOLS), (
        f"write-capable tools must not be polled: {sorted(set(TOOLS) & WRITE_TOOLS)}"
    )
    assert not (set(TOOLS) & AUTH_OR_LOCAL), (
        f"auth/local-only tools must not be polled: {sorted(set(TOOLS) & AUTH_OR_LOCAL)}"
    )
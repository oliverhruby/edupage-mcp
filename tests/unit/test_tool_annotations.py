"""Unit tests for the MCP tool annotation registry.

Rule 3 analogue for annotations: the registry must cover every @_tool exactly
once, and the read/write classification must agree with the docstrings, which
are the source of truth for whether a tool mutates state.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import edupage_mcp
from edupage_mcp import TOOL_ANNOTATIONS

SRC = Path(edupage_mcp.__file__).read_text(encoding="utf-8")
LINES = SRC.split("\n")


def _tool_names() -> list[str]:
    """Every function decorated with @_tool, in source order."""
    names = []
    for i, line in enumerate(LINES):
        if line.strip() == "@_tool":
            for j in range(i + 1, min(i + 6, len(LINES))):
                m = re.match(r"(?:async )?def ([a-z_0-9]+)\(", LINES[j])
                if m:
                    names.append(m.group(1))
                    break
    return names


TOOLS = _tool_names()


def _first_docstring_line(name: str) -> str:
    for i, line in enumerate(LINES):
        if re.match(r"(?:async )?def %s\(" % re.escape(name), line):
            for k in range(i + 1, min(i + 12, len(LINES))):
                s = LINES[k].strip()
                if s.startswith('"""'):
                    return s[3:].strip()
    return ""


def test_tools_are_discovered():
    # Guards against the parser silently returning [] and making every
    # coverage test below vacuously pass.
    assert len(TOOLS) == 31
    assert "login" in TOOLS and "get_meals" in TOOLS


def test_every_tool_has_annotations():
    missing = [n for n in TOOLS if n not in TOOL_ANNOTATIONS]
    assert missing == []


def test_no_orphan_registry_entries():
    orphans = [n for n in TOOL_ANNOTATIONS if n not in TOOLS]
    assert orphans == []


def test_registry_keys_match_tools_exactly():
    assert set(TOOL_ANNOTATIONS) == set(TOOLS)


def test_all_four_hints_present_on_every_entry():
    expected = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    for name, hints in TOOL_ANNOTATIONS.items():
        assert set(hints) == expected, name
        assert all(isinstance(v, bool) for v in hints.values()), name


def test_read_only_tools_are_marked_read_only():
    """A docstring saying 'Read-only' must not carry readOnlyHint=False."""
    offenders = []
    for name in TOOLS:
        doc = _first_docstring_line(name)
        if re.search(r"\bread-?only\b", doc, re.I) and not TOOL_ANNOTATIONS[name]["readOnlyHint"]:
            offenders.append(name)
    assert offenders == []


def test_write_tools_are_not_marked_read_only():
    """A docstring declaring a write must not claim readOnlyHint=True."""
    offenders = []
    for name in TOOLS:
        doc = _first_docstring_line(name)
        if re.search(r"\bwrites\b", doc, re.I) and TOOL_ANNOTATIONS[name]["readOnlyHint"]:
            offenders.append(name)
    assert offenders == []


def test_read_only_tools_are_non_destructive():
    offenders = [n for n, h in TOOL_ANNOTATIONS.items()
                 if h["readOnlyHint"] and h["destructiveHint"]]
    assert offenders == []


def test_destructive_writes_are_not_read_only():
    offenders = [n for n, h in TOOL_ANNOTATIONS.items()
                 if h["destructiveHint"] and h["readOnlyHint"]]
    assert offenders == []


def test_idempotent_reads():
    """Every read-only tool must be idempotent - a retry changes nothing."""
    offenders = [n for n, h in TOOL_ANNOTATIONS.items()
                 if h["readOnlyHint"] and not h["idempotentHint"]]
    assert offenders == []


def test_local_only_tools_are_closed_world():
    """switch_to_* / clear_student_cache never leave the process."""
    local = {"switch_to_student", "switch_to_parent", "clear_student_cache"}
    for n in local:
        assert TOOL_ANNOTATIONS[n]["openWorldHint"] is False, n


def test_network_tools_are_open_world():
    """Anything that talks to edupage.org must be openWorldHint=True."""
    local = {"switch_to_student", "switch_to_parent", "clear_student_cache"}
    offenders = [n for n, h in TOOL_ANNOTATIONS.items()
                 if n not in local and not h["openWorldHint"]]
    assert offenders == []


def test_known_risky_tools_are_destructive():
    """Tools that remove, overwrite or duplicate state are flagged."""
    for n in ("rate_meal", "sign_off_meal", "send_message",
              "download_homework_file", "custom_request"):
        assert TOOL_ANNOTATIONS[n]["destructiveHint"] is True, n


def test_send_message_is_not_idempotent():
    # Each call posts another message; there is no send-once semantics.
    assert TOOL_ANNOTATIONS["send_message"]["idempotentHint"] is False
    assert TOOL_ANNOTATIONS["custom_request"]["idempotentHint"] is False


def test_login_is_idempotent():
    """Re-logging in replaces the same session rather than stacking one."""
    for n in ("login", "login_all", "two_factor_finish"):
        assert TOOL_ANNOTATIONS[n]["idempotentHint"] is True, n


def test_new_homework_tools_classified():
    assert TOOL_ANNOTATIONS["get_homework_material"]["readOnlyHint"] is True
    assert TOOL_ANNOTATIONS["download_homework_file"]["readOnlyHint"] is False
    assert TOOL_ANNOTATIONS["download_homework_file"]["destructiveHint"] is True


def test_annotations_reach_the_wire():
    """ToolAnnotations must be importable and constructible from the registry.

    Skips when mcp is absent, since FastMCP is optional by design.
    """
    ToolAnnotations = getattr(edupage_mcp, "ToolAnnotations", None)
    if ToolAnnotations is None:
        pytest.skip("mcp not installed")
    for name, hints in TOOL_ANNOTATIONS.items():
        ann = ToolAnnotations(**hints)
        assert ann.readOnlyHint == hints["readOnlyHint"], name


def test_tool_wrapper_passes_annotations(monkeypatch):
    """_tool() must forward annotations to server.tool(), not drop them."""
    captured = {}

    class FakeServer:
        def tool(self, **kwargs):
            captured.update(kwargs)

            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(edupage_mcp, "server", FakeServer())
    monkeypatch.setattr(edupage_mcp, "ToolAnnotations", lambda **kw: kw)

    def get_meals():
        """doc"""
        return {}

    edupage_mcp._tool(get_meals)
    ann = captured.get("annotations")
    assert ann is not None
    assert ann["readOnlyHint"] is True


def test_tool_wrapper_tolerates_missing_annotations(monkeypatch):
    """A tool absent from the registry must still register, just unannotated."""
    captured = {}

    class FakeServer:
        def tool(self, **kwargs):
            captured.update(kwargs)

            def deco(fn):
                return fn
            return deco

    monkeypatch.setattr(edupage_mcp, "server", FakeServer())

    def brand_new_tool():
        """doc"""
        return {}

    edupage_mcp._tool(brand_new_tool)
    assert "annotations" not in captured or captured["annotations"] is None

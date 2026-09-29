"""Unit tests for the Glama watchdog's helpers.

Covers the README badge drift check so a stale grade letter cannot pass CI.
Run with: python -m pytest tests/unit/test_check_glama_quality.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "check_glama_quality.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_glama_quality", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_glama_quality"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cgq():
    if not _SCRIPT.exists():
        pytest.skip("check_glama_quality.py not present")
    return _load_module()


BADGE = (
    "[![Glama Grade](https://img.shields.io/badge/Glama-A-success)]"
    "(https://glama.ai/mcp/servers/oliverhruby/edupage-mcp)"
)


def _write_readme(tmp_path, monkeypatch, text):
    readme = tmp_path / "README.md"
    readme.write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return readme


def test_all_a_matches_badge(cgq, tmp_path, monkeypatch):
    _write_readme(tmp_path, monkeypatch, BADGE)
    assert cgq.check_readme_badge([("get_grades", "A"), ("login", "A")]) == []


def test_single_downgrade_is_reported(cgq, tmp_path, monkeypatch):
    _write_readme(tmp_path, monkeypatch, BADGE)
    notes = cgq.check_readme_badge([("get_grades", "A"), ("login", "B")])
    assert len(notes) == 1
    assert "says A" in notes[0] and "worst tool grade is B" in notes[0]


def test_multiple_downgrades_reports_worst_letter(cgq, tmp_path, monkeypatch):
    _write_readme(tmp_path, monkeypatch, BADGE)
    notes = cgq.check_readme_badge([("a", "A"), ("b", "C"), ("c", "B")])
    assert len(notes) == 1
    # C is worse than B; the report must not settle for the alphabetically first
    assert "worst tool grade is C" in notes[0]
    assert "A, B, C" in notes[0]


def test_lowercase_letter_in_badge_is_accepted(cgq, tmp_path, monkeypatch):
    _write_readme(
        tmp_path,
        monkeypatch,
        BADGE.replace("Glama-A-success", "Glama-a-brightgreen"),
    )
    assert cgq.check_readme_badge([("a", "A")]) == []


def test_missing_badge_is_reported(cgq, tmp_path, monkeypatch):
    _write_readme(tmp_path, monkeypatch, "# no badge here\n")
    notes = cgq.check_readme_badge([("a", "A")])
    assert len(notes) == 1
    assert "no Glama grade" in notes[0]


def test_no_tools_skips_silently(cgq, tmp_path, monkeypatch):
    _write_readme(tmp_path, monkeypatch, "# no badge here\n")
    assert cgq.check_readme_badge([]) == []


def test_unreadable_readme_is_skipped_not_fatal(cgq, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no README.md at all
    notes = cgq.check_readme_badge([("a", "A")])
    assert len(notes) == 1
    assert "skipped" in notes[0]


def test_parse_tools_reads_letters_from_sample_html(cgq):
    html = (
        '<a href="/mcp/servers/x/y/tools/get_grades">get_grades</a>'
        '<span class="g">A</span>'
        '<a href="/mcp/servers/x/y/tools/login">login</a>'
        '<span class="g">A</span>'
    )
    assert cgq.parse_tools(html) == [("get_grades", "A"), ("login", "A")]

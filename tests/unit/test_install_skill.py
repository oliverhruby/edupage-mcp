"""Unit tests for the `install-skill` CLI subcommand.

The skill used to be installable only by hand: the README told users to
`mkdir -p ~/.config/opencode/skills/school-day-summary` and then `cp` from a
literal `<site-packages>` placeholder that nothing could resolve. `main()` now
dispatches `install-skill` on argv[1] and copies the file with
`importlib.resources`, so the README is a real command.

Two contracts matter most and are easy to regress:

1. **The zero-argument server start is byte-identical.** `main()` is the MCP
   entry point *and* the Docker ENTRYPOINT (`python -m edupage_mcp`); if
   argparse or the installer ever runs on that path, every MCP client and the
   container break. `test_zero_argv_main_starts_the_server` proves the branch is
   not taken by giving the module a fake home and asserting nothing was written.
2. **Re-running must not clobber a user's edits.** Identical content is a
   no-op, a differing file is refused unless `--force` says otherwise.

Every test runs against a `tmp_path` home via monkeypatched `Path.home()`;
nothing here reads or writes the real user profile.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import edupage_mcp as m

SKILL = "school-day-summary"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Redirect `Path.home()` at a throwaway directory.

    `Path.home` is the only home lookup the installer uses, so patching the
    class method redirects both target resolution and agent detection at once.
    """
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


@pytest.fixture()
def agent_roots(home):
    """Create the agent home directories and return `{agent: root path}`."""
    return {
        agent: home / rel.parent
        for agent, rel in m._AGENT_SKILL_DIRS.items()
    }


def _run(argv, capsys) -> tuple[int, str, str]:
    """Run install-skill, returning `(exit_code, stdout, stderr)`."""
    code = m._cli_install_skill(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _skill_bytes() -> bytes:
    with m._skill_source_path() as path:
        return path.read_bytes()


# --------------------------------------------------------------------------
# Target directories
# --------------------------------------------------------------------------
def test_all_four_agents_are_mapped():
    """opencode, copilot, claude and the cross-vendor ~/.agents location.

    `agents` is not a fallback: Copilot, Codex and Cursor all read
    ~/.agents/skills/, so dropping it would silently lose installs.
    """
    assert set(m._AGENT_SKILL_DIRS) == {"opencode", "copilot", "claude", "agents"}
    assert m._AGENT_ORDER == ("opencode", "copilot", "claude", "agents")
    assert m._AGENT_CHOICES == ("opencode", "copilot", "claude", "agents", "all")


@pytest.mark.parametrize("agent, expected", [
    ("opencode", Path(".config") / "opencode" / "skills" / SKILL / "SKILL.md"),
    ("copilot", Path(".copilot") / "skills" / SKILL / "SKILL.md"),
    ("claude", Path(".claude") / "skills" / SKILL / "SKILL.md"),
    ("agents", Path(".agents") / "skills" / SKILL / "SKILL.md"),
])
def test_agent_path_mapping(agent, expected, home, capsys):
    (home / m._AGENT_SKILL_DIRS[agent].parent).mkdir(parents=True)

    code, out, _ = _run(["--agent", agent], capsys)

    target = home / expected
    assert code == 0
    assert target.is_file()
    assert target.read_bytes() == _skill_bytes()
    assert f"installed -> {target}" in out


def test_auto_detects_only_existing_agent_dirs(home, capsys):
    """With no --agent, only agents that are already set up get touched."""
    (home / ".copilot").mkdir()
    (home / m._AGENT_SKILL_DIRS["opencode"].parent).mkdir(parents=True)

    code, out, _ = _run([], capsys)

    assert code == 0
    assert (home / ".copilot" / "skills" / SKILL / "SKILL.md").is_file()
    assert (home / m._AGENT_SKILL_DIRS["opencode"] / SKILL / "SKILL.md").is_file()
    assert not (home / ".claude").exists()
    assert not (home / ".agents").exists()
    assert out.count("installed ->") == 2


def test_agent_all_creates_every_directory(home, capsys):
    """`--agent all` is the escape hatch for a machine with no agents yet."""
    code, out, _ = _run(["--agent", "all"], capsys)

    assert code == 0
    assert out.count("installed ->") == len(m._AGENT_ORDER)
    for agent in m._AGENT_ORDER:
        assert (home / m._AGENT_SKILL_DIRS[agent] / SKILL / "SKILL.md").is_file()


def test_no_agent_directory_found_is_a_hard_failure(home, capsys):
    """Silent success when nothing was installed would be the worst outcome."""
    code, out, err = _run([], capsys)

    assert code != 0
    assert out == ""
    for agent in m._AGENT_ORDER:
        assert "--agent" in err and agent in err


# --------------------------------------------------------------------------
# Idempotency / clobber protection
# --------------------------------------------------------------------------
def test_second_run_reports_identical_and_rewrites_nothing(home, capsys):
    (home / ".copilot").mkdir()
    assert _run([], capsys)[0] == 0
    target = home / ".copilot" / "skills" / SKILL / "SKILL.md"
    before = target.stat().st_mtime_ns

    code, out, _ = _run([], capsys)

    assert code == 0
    assert f"skipped (identical) -> {target}" in out
    assert target.stat().st_mtime_ns == before, "identical file was rewritten"


def test_differing_file_is_refused_without_force(home, capsys):
    (home / ".copilot").mkdir()
    target = home / ".copilot" / "skills" / SKILL / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("locally edited skill\n", encoding="utf-8")

    code, out, _ = _run([], capsys)

    assert code == 0, "a refusal is a no-op, not a failure"
    assert f"skipped (exists, differs, use --force) -> {target}" in out
    assert target.read_text(encoding="utf-8") == "locally edited skill\n"


def test_differing_file_is_overwritten_with_force(home, capsys):
    (home / ".copilot").mkdir()
    target = home / ".copilot" / "skills" / SKILL / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("locally edited skill\n", encoding="utf-8")

    code, out, _ = _run(["--force"], capsys)

    assert code == 0
    assert f"installed -> {target}" in out
    assert target.read_bytes() == _skill_bytes()


def test_force_does_not_disturb_other_agents(home, capsys):
    """--agent scopes --force to that agent, not to every detected one."""
    (home / ".copilot").mkdir()
    (home / ".claude").mkdir()
    edited = home / ".claude" / "skills" / SKILL / "SKILL.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("mine\n", encoding="utf-8")

    code, out, _ = _run(["--agent", "copilot", "--force"], capsys)

    assert code == 0
    assert edited.read_text(encoding="utf-8") == "mine\n"
    assert not out.strip().count("claude")


# --------------------------------------------------------------------------
# --print and --dry-run must not write
# --------------------------------------------------------------------------
def test_print_writes_nothing(home, capsys):
    (home / ".copilot").mkdir()

    code, out, err = _run(["--print"], capsys)

    assert code == 0
    assert err == ""
    with m._skill_source_path() as source:
        assert out == f"{source}\n"
        assert source.is_file()
    assert list(home.iterdir()) == [home / ".copilot"], "nothing may be created"
    assert not (home / ".copilot" / "skills").exists()


def test_print_ignores_agent_selection(home, capsys):
    """--print resolves the source, so --agent is a no-op, not a second action."""
    code, out, _ = _run(["--agent", "copilot", "--print"], capsys)

    assert code == 0
    assert out.strip().endswith("SKILL.md")
    assert not (home / ".copilot").exists()


def test_dry_run_reports_without_writing(home, capsys):
    (home / ".copilot").mkdir()

    code, out, _ = _run(["--agent", "copilot", "--dry-run"], capsys)

    target = home / ".copilot" / "skills" / SKILL / "SKILL.md"
    assert code == 0
    assert f"would install -> {target}" in out
    assert not target.exists()
    assert not (home / ".copilot" / "skills").exists(), "dry run created dirs"


def test_dry_run_reports_overwrite_for_a_differing_file(home, capsys):
    (home / ".copilot").mkdir()
    target = home / ".copilot" / "skills" / SKILL / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("mine\n", encoding="utf-8")

    code, out, _ = _run(["--agent", "copilot", "--dry-run", "--force"], capsys)

    assert code == 0
    assert f"would overwrite -> {target}" in out
    assert target.read_text(encoding="utf-8") == "mine\n"


def test_dry_run_reports_identical_when_already_installed(home, capsys):
    (home / ".copilot").mkdir()
    assert _run(["--agent", "copilot"], capsys)[0] == 0

    code, out, _ = _run(["--agent", "copilot", "--dry-run"], capsys)

    assert code == 0
    assert "skipped (identical) ->" in out


# --------------------------------------------------------------------------
# Bad input
# --------------------------------------------------------------------------
def test_unknown_flag_exits_non_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        m._cli_install_skill(["--nope"])
    assert exc.value.code != 0


# --------------------------------------------------------------------------
# main() dispatch
# --------------------------------------------------------------------------
def test_dispatch_ignores_unrecognised_arguments():
    """No recognised subcommand => None => the caller starts the server."""
    assert m._dispatch_cli([]) is None
    assert m._dispatch_cli(["--help"]) is None
    assert m._dispatch_cli(["serve"]) is None


def test_dispatch_routes_install_skill(monkeypatch, capsys):
    monkeypatch.setattr(m, "_cli_install_skill", lambda argv: 42)
    assert m._dispatch_cli(["install-skill", "--agent", "claude"]) == 42


def test_zero_argv_main_starts_the_server_not_the_installer(monkeypatch, home):
    """The regression that matters most.

    `main()` is both the console script and the Docker ENTRYPOINT. With an
    empty argv it must reach `server.run()` and touch no filesystem: a fake
    home with no agent directory in it means the installer branch cannot run
    without failing loudly.
    """
    started = {}

    class FakeServer:
        def run(self, transport=None, **kwargs):
            started["transport"] = transport

    saved_clients = dict(m._clients)
    monkeypatch.setattr(m, "server", FakeServer())
    monkeypatch.setattr(m, "EDUPAGE_USERNAME", "")
    monkeypatch.setattr(m, "EDUPAGE_PASSWORD", "")
    monkeypatch.setattr(m, "EDUPAGE_SUBDOMAINS", "")
    monkeypatch.setattr(m, "MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(m, "_MCP_PORT_ERROR", None)
    monkeypatch.setattr(sys, "argv", ["edupage-mcp-full"])
    m._clients.clear()
    try:
        m.main()
    finally:
        m._clients.clear()
        m._clients.update(saved_clients)

    assert started.get("transport") == "stdio"
    assert list(home.iterdir()) == [], "the zero-arg server path wrote something"


def test_main_routes_install_skill_subcommand(monkeypatch, tmp_path):
    """The mirror image: a recognised subcommand never starts a server."""
    started = []

    class FakeServer:
        def run(self, *a, **kw):
            started.append(kw)

    monkeypatch.setattr(m, "server", FakeServer())
    monkeypatch.setattr(sys, "argv", ["edupage-mcp-full", "install-skill", "--print"])

    with pytest.raises(SystemExit) as exc:
        m.main()

    assert exc.value.code == 0
    assert started == []


def test_module_entrypoint_module_main_is_the_same_call():
    """`python -m edupage_mcp` must go through main(), not a second code path."""
    import edupage_mcp.__main__ as entry

    assert entry.main is m.main
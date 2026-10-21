"""Unit tests for the one-click plugin packaging and the skills mirrors.

Four separate contracts are covered here:

1. ``scripts/sync_skills.py`` keeps every mirror — ``plugin/skills/`` (Agent
   Plugins 1.0) and ``.apm/skills/`` (APM/OpenAPM) — byte-identical to the
   canonical ``src/edupage_mcp/skills/``. Drift in either direction fails.
2. The two plugin manifests follow the specs they claim: ``plugin.json`` uses
   only the CLOSED Agent Plugins 1.0 key set, and ``mcp.json`` launches the
   public PyPI form of the server rather than a developer's local checkout.
3. The canonical ``SKILL.md`` frontmatter satisfies the Agent Skills spec.
4. The root ``apm.yml`` stays version-aligned with ``pyproject.toml`` and
   declares the same self-defined stdio MCP server as ``plugin/mcp.json``.

Nothing here mutates the real repository: the drift tests run against
temporary trees, and the byte-identity test only reads.

Run with: python -m pytest tests/unit/test_plugin_sync.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import sys
from contextlib import redirect_stdout

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
_SYNC_SCRIPT = REPO / "scripts" / "sync_skills.py"

CANONICAL_SKILL = REPO / "src" / "edupage_mcp" / "skills" / "school-day-summary" / "SKILL.md"
MIRRORED_SKILL = REPO / "plugin" / "skills" / "school-day-summary" / "SKILL.md"
APM_SKILL = REPO / ".apm" / "skills" / "school-day-summary" / "SKILL.md"
APM_MANIFEST = REPO / "apm.yml"
PLUGIN_JSON = REPO / "plugin" / "plugin.json"
MCP_JSON = REPO / "plugin" / "mcp.json"
CLAUDE_PLUGIN_JSON = REPO / "plugin" / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO / ".claude-plugin" / "marketplace.json"

# Agent Plugins 1.0 §5.2: "The manifest MUST be JSON and MUST contain a
# top-level object. Its schema is closed: the only permitted top-level fields
# are ..." - no `skills` / `mcpServers` key may appear, the component locations
# are fixed and unconfigurable (§6.1).
AGENT_PLUGINS_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_PLUGINS_MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
AGENT_PLUGINS_MANIFEST_KEYS = {
    "$schema", "name", "version", "description", "author",
    "homepage", "repository", "license", "keywords", "extensions",
}

# Agent Skills spec frontmatter key set.
SKILL_REQUIRED_KEYS = {"name", "description"}
SKILL_OPTIONAL_KEYS = {"license", "compatibility", "metadata", "allowed-tools"}
MAX_DESCRIPTION_CHARS = 1024
MAX_COMPATIBILITY_CHARS = 500
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Never allowed in a committed file: the maintainer's local virtualenv and a
# Windows user home directory are personal-machine config, not package data.
FORBIDDEN_SUBSTRINGS = (".venv", "Users\\oliver.hruby", "/home/oliver", ".venv-a")


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_skills", _SYNC_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_skills"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sync_skills():
    if not _SYNC_SCRIPT.exists():
        pytest.skip("sync_skills.py not present")
    return _load_module()


@pytest.fixture(scope="module")
def manifests():
    return {
        "plugin.json": json.loads(PLUGIN_JSON.read_text(encoding="utf-8")),
        "mcp.json": json.loads(MCP_JSON.read_text(encoding="utf-8")),
        "claude-plugin.json": json.loads(CLAUDE_PLUGIN_JSON.read_text(encoding="utf-8")),
        "marketplace.json": json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8")),
    }


def _run(argv):
    """Run the sync script's ``main`` and return ``(exit_code, stdout)``."""
    module = _load_module()
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = module.main(argv)
    return code, buf.getvalue()


def _copy_tree(src: pathlib.Path, dst: pathlib.Path) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_file():
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


def _mentions(out: str, path: pathlib.Path) -> bool:
    """True when *out* names *path* (native separators outside the repo root,
    POSIX separators inside it)."""
    return path.as_posix() in out or str(path) in out


# --------------------------------------------------------------------------
# sync_skills.py
# --------------------------------------------------------------------------


def test_check_passes_on_the_committed_tree(sync_skills):
    """The committed mirror must already be in sync -- CI runs --check."""
    code, out = _run(["--check"])
    assert code == 0, out
    assert "OK:" in out


def test_check_exits_1_on_a_single_byte_perturbation(sync_skills, tmp_path):
    """One flipped byte anywhere in the mirror must fail the check."""
    source = tmp_path / "canonical" / "skills"
    dest = tmp_path / "plugin" / "skills"
    _copy_tree(CANONICAL_SKILL.parent.parent, source)
    _copy_tree(source, dest)

    mirrored = dest / "school-day-summary" / "SKILL.md"
    original = mirrored.read_bytes()
    mirrored.write_bytes(original[:-2] + b"X" + original[-1:])  # same length, one byte differs
    assert mirrored.read_bytes() != original

    code, out = _run(["--check", "--source", str(source), "--dest", str(dest)])
    assert code == 1
    assert "DRIFT" in out
    assert "missing/changed" in out
    assert _mentions(out, mirrored)


def test_check_flags_a_deleted_mirror_file(sync_skills, tmp_path):
    source = tmp_path / "canonical" / "skills"
    dest = tmp_path / "plugin" / "skills"
    _copy_tree(CANONICAL_SKILL.parent.parent, source)
    _copy_tree(source, dest)
    mirrored = dest / "school-day-summary" / "SKILL.md"
    mirrored.unlink()

    code, out = _run(["--check", "--source", str(source), "--dest", str(dest)])
    assert code == 1
    assert _mentions(out, mirrored)


def test_check_flags_a_stale_extra_file(sync_skills, tmp_path):
    """A leftover file in the mirror is drift a one-directional copy would miss."""
    source = tmp_path / "canonical" / "skills"
    dest = tmp_path / "plugin" / "skills"
    _copy_tree(CANONICAL_SKILL.parent.parent, source)
    _copy_tree(source, dest)
    stale = dest / "school-day-summary" / "OLD.md"
    stale.write_text("gone\n", encoding="utf-8")

    code, out = _run(["--check", "--source", str(source), "--dest", str(dest)])
    assert code == 1
    assert "stale" in out
    assert _mentions(out, stale)


def test_write_recreates_the_tree_and_is_idempotent(sync_skills, tmp_path):
    source = tmp_path / "canonical" / "skills"
    dest = tmp_path / "plugin" / "skills"
    _copy_tree(CANONICAL_SKILL.parent.parent, source)

    assert dest.exists() is False  # parents do not exist yet
    code, _ = _run(["--source", str(source), "--dest", str(dest)])
    assert code == 0
    assert (dest / "school-day-summary" / "SKILL.md").read_bytes() == CANONICAL_SKILL.read_bytes()

    code, out = _run(["--source", str(source), "--dest", str(dest)])  # re-run is a no-op
    assert code == 0
    assert "Wrote 0 file(s)" in out


def test_write_removes_a_stale_file(sync_skills, tmp_path):
    source = tmp_path / "canonical" / "skills"
    dest = tmp_path / "plugin" / "skills"
    _copy_tree(CANONICAL_SKILL.parent.parent, source)
    _copy_tree(source, dest)
    stale = dest / "school-day-summary" / "OLD.md"
    stale.write_text("gone\n", encoding="utf-8")

    code, _ = _run(["--source", str(source), "--dest", str(dest)])
    assert code == 0
    assert not stale.exists()


def test_write_rejects_a_missing_canonical_tree(sync_skills, tmp_path):
    code, out = _run(["--check", "--source", str(tmp_path / "nope"), "--dest", str(tmp_path)])
    assert code == 1
    assert "not found" in out


# --------------------------------------------------------------------------
# Mirror integrity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mirror", [MIRRORED_SKILL, APM_SKILL], ids=["plugin", "apm"])
def test_canonical_and_mirrored_skill_are_byte_identical(mirror):
    assert mirror.is_file(), "run: python scripts/sync_skills.py"
    assert mirror.read_bytes() == CANONICAL_SKILL.read_bytes()


# --------------------------------------------------------------------------
# APM manifest (apm.yml)
# --------------------------------------------------------------------------


def test_apm_manifest_version_matches_pyproject():
    """pyproject.toml is the version source of truth; apm.yml follows it."""
    text = APM_MANIFEST.read_text(encoding="utf-8")
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert match, "no version in pyproject.toml"
    apm_version = re.search(r'^version:\s*["\']?([^"\'\s]+)', text, re.MULTILINE)
    assert apm_version, "no version in apm.yml"
    assert apm_version.group(1) == match.group(1)


def test_apm_manifest_declares_the_same_mcp_server_as_the_plugin(manifests):
    """apm.yml's self-defined stdio entry must mirror plugin/mcp.json."""
    text = APM_MANIFEST.read_text(encoding="utf-8")
    server = manifests["mcp.json"]["mcpServers"]["edupage"]
    assert "name: edupage" in text
    assert "registry: false" in text
    assert f"transport: {server['type']}" in text
    assert f"command: {server['command']}" in text
    assert "edupage-mcp-full" in server["args"]


def test_apm_manifest_carries_no_absolute_paths_or_local_secrets():
    """Personal-machine config must never reach a public file."""
    text = APM_MANIFEST.read_text(encoding="utf-8")
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in text, f"apm.yml leaks {bad!r}"


# --------------------------------------------------------------------------
# Agent Plugins 1.0 manifests
# --------------------------------------------------------------------------


def test_plugin_json_uses_only_closed_schema_keys(manifests):
    """Agent Plugins 1.0 §5.2 -- the schema is closed (additionalProperties false)."""
    keys = set(manifests["plugin.json"])
    assert keys <= AGENT_PLUGINS_MANIFEST_KEYS, f"non-schema keys: {sorted(keys - AGENT_PLUGINS_MANIFEST_KEYS)}"
    # The component locations are fixed and unconfigurable (§6.1): a manifest
    # that tried to declare them would be silently ignored, so it must not.
    assert "skills" not in keys
    assert "mcpServers" not in keys


def test_plugin_json_has_the_required_fields(manifests):
    plugin = manifests["plugin.json"]
    assert plugin["$schema"] == AGENT_PLUGINS_SCHEMA_ID
    assert plugin["name"] == "edupage"
    # §5.5 name constraints -- plain kebab-case, no namespace prefix. Copilot
    # silently skips a plugin whose name is not plain kebab-case.
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*(\.[a-z0-9]+(-[a-z0-9]+)*)*", plugin["name"])
    assert 1 <= len(plugin["name"]) <= 64


def test_mcp_json_declares_the_agent_plugins_mcp_schema(manifests):
    """§7.2.1 -- closed top level: $schema + mcpServers, nothing else."""
    mcp = manifests["mcp.json"]
    assert set(mcp) == {"$schema", "mcpServers"}
    # §10.1 -- the MCP schema version must match the plugin manifest's.
    assert mcp["$schema"] == AGENT_PLUGINS_MCP_SCHEMA_ID
    assert mcp["$schema"].split("/")[-2] == AGENT_PLUGINS_SCHEMA_ID.split("/")[-2]


def test_mcp_json_launches_the_public_uvx_form(manifests):
    """`uvx edupage-mcp-full` from PyPI -- never a local checkout."""
    server = manifests["mcp.json"]["mcpServers"]["edupage"]
    assert server["type"] == "stdio"
    assert server["command"] == "uvx"
    assert server["args"] == ["edupage-mcp-full"]
    # §7.2.1 stdio variant is a closed union -- no strays.
    assert set(server) <= {"type", "command", "args", "env", "cwd"}


@pytest.mark.parametrize("name", ["plugin.json", "mcp.json", "claude-plugin.json", "marketplace.json"])
def test_manifests_carry_no_absolute_paths_or_local_secrets(manifests, name):
    """Personal-machine config must never reach a public file."""
    text = json.dumps(manifests[name])
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in text, f"{name} leaks {bad!r}"
    for literal in ("hashed_secret", "BEGIN PRIVATE KEY", "sk-"):
        assert literal not in text, f"{name} looks like it embeds a credential"


@pytest.mark.parametrize("name", ["mcp.json", "claude-plugin.json"])
def test_credential_env_vars_never_carry_a_literal_value(manifests, name):
    """Agent Plugins §9.2: env values are visible package data, never a secret
    mechanism. Claude substitutes from its secure store; the portable manifest
    inherits the ambient environment instead."""
    servers = manifests[name]["mcpServers"].values()
    for server in servers:
        for var, value in (server.get("env") or {}).items():
            assert var in ("EDUPAGE_USERNAME", "EDUPAGE_PASSWORD", "EDUPAGE_SUBDOMAINS"), var
            assert value.startswith("${user_config."), f"{name}: {var} holds a literal {value!r}"


def test_portable_mcp_json_declares_no_credentials_at_all(manifests):
    """Agent Plugins 1.0 defines no portable credential-reference field and has
    no env-var interpolation, so the portable manifest must stay silent about
    the credentials rather than clobbering the ambient environment."""
    for server in manifests["mcp.json"]["mcpServers"].values():
        assert "env" not in server, "an empty/literal env block would override the user's environment"


def test_plugin_version_matches_pyproject(manifests):
    """pyproject.toml is the version source of truth; every manifest follows it."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert match, "no version in pyproject.toml"
    expected = match.group(1)
    assert manifests["plugin.json"]["version"] == expected
    assert manifests["claude-plugin.json"]["version"] == expected


# --------------------------------------------------------------------------
# Claude Code plugin + marketplace
# --------------------------------------------------------------------------


def test_claude_plugin_uses_user_config_for_credentials(manifests):
    """Claude has a richer surface: prompt for credentials, store the password securely."""
    plugin = manifests["claude-plugin.json"]
    options = plugin["userConfig"]
    assert options["edupage_password"]["sensitive"] is True
    assert options["edupage_username"].get("sensitive") is not True

    env = plugin["mcpServers"]["edupage"]["env"]
    for var, key in (
        ("EDUPAGE_USERNAME", "edupage_username"),
        ("EDUPAGE_PASSWORD", "edupage_password"),
        ("EDUPAGE_SUBDOMAINS", "edupage_subdomains"),
    ):
        assert env[var] == f"${{user_config.{key}}}"
        assert key in options, f"{key} referenced but not declared in userConfig"


def test_claude_plugin_also_launches_the_public_uvx_form(manifests):
    server = manifests["claude-plugin.json"]["mcpServers"]["edupage"]
    assert server["command"] == "uvx"
    assert server["args"] == ["edupage-mcp-full"]


def test_marketplace_has_one_edupage_entry_pointing_at_the_plugin_dir(manifests):
    """A relative-path source keeps SKILL.md at two copies, not three."""
    marketplace = manifests["marketplace.json"]
    assert {"name", "owner", "plugins"} <= set(marketplace)
    entries = [p for p in marketplace["plugins"] if p["name"] == "edupage"]
    assert len(entries) == 1
    assert entries[0]["source"] == "./plugin"
    # The entry must not restate the version: Claude warns when an entry and the
    # plugin's own plugin.json both set it, which would fail `validate --strict`.
    assert "version" not in entries[0]
    # The entry stays a manifest-only reference so plugin.json remains the authority.
    assert not ({"commands", "agents", "skills", "hooks"} & set(entries[0]))


def test_claude_manifest_lives_inside_the_plugin_directory():
    """Claude reads <plugin root>/.claude-plugin/plugin.json, so it must sit in plugin/."""
    assert CLAUDE_PLUGIN_JSON.is_file()
    assert CLAUDE_PLUGIN_JSON.parent.parent == REPO / "plugin"
    # One directory, two standards, one copy of SKILL.md.
    assert not (REPO / ".claude-plugin" / "plugin.json").exists()


# --------------------------------------------------------------------------
# Agent Skills frontmatter
# --------------------------------------------------------------------------


def _frontmatter(text: str) -> dict:
    """Parse SKILL.md frontmatter without a YAML dependency.

    Deliberately minimal: it handles the scalars and the one nested mapping
    (``metadata:``) that the Agent Skills spec allows here. Scalars that YAML
    would resolve to a non-string (bare ``true``, ``1.0``, ``[a, b]``) are
    returned as ``_Scalar`` so a test can assert the spec's "map from string
    keys to string values" rule instead of silently coercing them.
    """
    if not text.startswith("---"):
        raise AssertionError("SKILL.md must start with a '---' frontmatter fence")
    _, block, _body = text.split("---", 2)
    data: dict = {}
    current_key = None
    for raw in block.strip().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")):  # nested key under metadata:
            key, _, value = raw.strip().partition(":")
            data[current_key][key.strip()] = _Scalar(value.strip())
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            data[key] = {}
            current_key = key
        else:
            data[key] = _Scalar(value)
            current_key = None
    return data


class _Scalar(str):
    """A raw frontmatter scalar; empty means YAML resolved it to a non-string."""

    __slots__ = ()

    def __new__(cls, raw: str):
        if raw in {"true", "false", "null", "~"} or re.fullmatch(r"[-+]?\d+(\.\d+)?", raw) \
                or raw[:1] in "[{":
            return super().__new__(cls, "")
        return super().__new__(cls, raw)


def test_skill_name_matches_directory_and_kebab_case():
    data = _frontmatter(CANONICAL_SKILL.read_text(encoding="utf-8"))
    name = str(data["name"])
    assert 1 <= len(name) <= 64
    assert SKILL_NAME_RE.fullmatch(name), f"{name!r} is not plain kebab-case"
    assert "--" not in name and not name.startswith("-") and not name.endswith("-")
    # The spec requires name == parent directory name.
    assert name == CANONICAL_SKILL.parent.name


def test_skill_description_is_present_and_within_the_limit():
    data = _frontmatter(CANONICAL_SKILL.read_text(encoding="utf-8"))
    description = str(data["description"])
    assert description.strip()
    assert len(description) <= MAX_DESCRIPTION_CHARS


def test_skill_frontmatter_uses_only_spec_fields():
    data = _frontmatter(CANONICAL_SKILL.read_text(encoding="utf-8"))
    assert SKILL_REQUIRED_KEYS <= set(data)
    assert set(data) <= SKILL_REQUIRED_KEYS | SKILL_OPTIONAL_KEYS


def test_skill_compatibility_is_within_the_limit():
    data = _frontmatter(CANONICAL_SKILL.read_text(encoding="utf-8"))
    compatibility = str(data["compatibility"])
    assert compatibility.strip()
    assert len(compatibility) <= MAX_COMPATIBILITY_CHARS


def test_skill_metadata_values_are_all_strings():
    """The spec requires string -> string. opencode's docs warn specifically that
    bare YAML booleans/numbers are non-portable here, so nothing is coerced."""
    data = _frontmatter(CANONICAL_SKILL.read_text(encoding="utf-8"))
    metadata = data["metadata"]
    assert metadata, "metadata map is empty"
    for key, value in metadata.items():
        assert isinstance(value, _Scalar), f"{key} is not a plain YAML scalar"
        assert str(value).strip(), f"{key} is a bare YAML non-string ({value!r})"
    assert {"mcp-server", "mcp-package"} <= set(metadata)
    assert str(metadata["mcp-server"]) == "edupage"
    assert str(metadata["mcp-package"]) == "edupage-mcp-full"


def test_skill_metadata_detects_a_bare_yaml_non_string():
    """Guards the guard: the parser above must actually reject `key: true`."""
    parsed = _frontmatter("---\nname: x\nmetadata:\n  flag: true\n  n: 1.5\n---\nbody\n")
    assert str(parsed["metadata"]["flag"]) == ""
    assert str(parsed["metadata"]["n"]) == ""
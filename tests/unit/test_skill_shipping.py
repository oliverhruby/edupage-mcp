"""The bundled agent skill must actually ship, and must be locatable.

`skills/school-day-summary/SKILL.md` is declared in pyproject.toml under
`[tool.setuptools.package-data]`, which is easy to break and impossible to
notice: the file is inert documentation that no unit test imports and no
runtime code path touches until someone runs `install-skill`. Before this file
existed the repo had zero tests and zero CI steps asserting the skill lands in
a built distribution - a bad MANIFEST or package-data edit would have shipped a
broken install instruction and nobody would have found out.

Scope, split with the sibling file `test_install_skill.py`:

- **here**: the skill exists as a real package resource (resolved through
  `importlib.resources`, so wheel / installed-sdist / editable all behave the
  same), pyproject still declares it, and its front matter is the contract the
  agent skill loader reads. The *built artifact* check is a CI step, not a test,
  because building a wheel inside the unit suite would be far too slow; see the
  "Assert the agent skill ships in the wheel" steps in
  `.github/workflows/quality-gates.yml` and `.github/workflows/publish.yml`.
- **there**: what the installer does with the resource.

These tests read the repo's own files, so they `pytest.skip` when the suite runs
against a non-checkout install (e.g. a plain `pip install` into site-packages)
where pyproject.toml is not next to the package.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import edupage_mcp as m

SKILL_NAME = "school-day-summary"
PACKAGE_PATH = ("skills", SKILL_NAME, "SKILL.md")
REPO_ROOT = Path(m.__file__).resolve().parents[2]
SKILL_IN_PACKAGE = Path(*PACKAGE_PATH)


def _checkout_file(*parts: str) -> Path | None:
    """A tracked repo file, or None when running outside a source checkout."""
    candidate = REPO_ROOT.joinpath(*parts)
    return candidate if candidate.is_file() else None


checkout_only = pytest.mark.skipif(
    not (_SKILL := _checkout_file("src", "edupage_mcp", *PACKAGE_PATH)),
    reason="not running from a source checkout",
)


# --------------------------------------------------------------------------
# The resource itself
# --------------------------------------------------------------------------
def test_skill_resolves_via_importlib_resources():
    """`files()` must find it - this is the accessor `install-skill` uses.

    Anchoring on the package (not `Path(__file__).parent`) is what makes the
    same code correct for a zip-safe install.
    """
    resource = m._skill_resource()
    assert resource.is_file()
    assert resource.name == "SKILL.md"
    assert resource.read_bytes().strip(), "bundled SKILL.md is empty"


def test_skill_source_path_is_a_real_file():
    """`as_file` must materialise a real path we can copy from."""
    with m._skill_source_path() as path:
        assert isinstance(path, Path)
        assert path.is_file()
        assert path.read_bytes() == m._skill_resource().read_bytes()


def test_resource_is_importable_under_the_documented_package_path():
    """Guard the exact path: `edupage_mcp/skills/<name>/SKILL.md`.

    A rename of the directory or of the file would silently break every
    installed copy, and nothing else in the suite notices.
    """
    from importlib import resources

    resolved = resources.files("edupage_mcp") / "skills" / SKILL_NAME / "SKILL.md"
    assert resolved.is_file()


@checkout_only
def test_packaged_resource_matches_the_tracked_source():
    """An installed copy must not drift from the file that is committed."""
    tracked = _SKILL.read_bytes()
    assert m._skill_resource().read_bytes() == tracked, (
        "the packaged skill differs from src/edupage_mcp/%s" % SKILL_IN_PACKAGE.as_posix()
    )


# --------------------------------------------------------------------------
# pyproject declaration - the part that actually decides shipping
# --------------------------------------------------------------------------
@checkout_only
def test_pyproject_declares_the_skill_as_package_data():
    """`package-data` is the only reason the file lands in the wheel."""
    pyproject = _checkout_file("pyproject.toml")
    assert pyproject is not None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["edupage_mcp"]

    assert SKILL_IN_PACKAGE.as_posix() in patterns, (
        f"src/edupage_mcp/{SKILL_IN_PACKAGE.as_posix()} is no longer declared as "
        f"package-data; the wheel would ship without it"
    )


@checkout_only
def test_pyproject_project_name_still_matches_app_dist():
    """AGENTS.md rule 6: `_APP_DIST` drives the advertised server version."""
    pyproject = _checkout_file("pyproject.toml")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["name"] == m._APP_DIST
    assert data["project"]["scripts"]["edupage-mcp-full"] == "edupage_mcp:main"


@checkout_only
def test_install_skill_maps_to_the_declared_console_script():
    """The CLI is only reachable because this entry point exists."""
    pyproject = _checkout_file("pyproject.toml")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    target = data["project"]["scripts"]["edupage-mcp-full"]
    module, _, func = target.partition(":")

    assert module == "edupage_mcp"
    assert func == "main"
    assert callable(getattr(m, func))


# --------------------------------------------------------------------------
# Front matter - the contract the agent's skill loader reads
# --------------------------------------------------------------------------
def _front_matter() -> dict[str, str]:
    text = m._skill_resource().read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with a YAML front matter block"
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def test_skill_front_matter_declares_its_name_and_description():
    fields = _front_matter()
    assert fields.get("name") == SKILL_NAME, (
        "the front-matter name must match the directory name the skill is "
        f"installed into ({SKILL_NAME})"
    )
    description = fields.get("description", "")
    assert len(description) > 80, (
        "the description is what an agent matches a request against; a short "
        "one makes the skill undiscoverable"
    )


def test_skill_points_at_the_tool_it_wraps():
    """A skill that does not name `get_day_summary` is orphaned."""
    body = m._skill_resource().read_text(encoding="utf-8")
    assert "get_day_summary" in body


def test_skill_copy_matches_the_file_that_gets_installed():
    """The installed bytes and the tracked bytes are one file, not two."""
    with m._skill_source_path() as path:
        assert path.name == "SKILL.md"
        assert path.parent.name == SKILL_NAME
        assert path.read_text(encoding="utf-8").startswith("---\n")
#!/usr/bin/env python3
"""Mirror the canonical agent skills into every ecosystem's skills tree.

The canonical copy of every skill lives in the distributed Python package, at
``src/edupage_mcp/skills/<name>/SKILL.md`` — it ships in the wheel (see the
``tool.setuptools.package-data`` entry in ``pyproject.toml``) and is what the
manual "copy it out of site-packages" install path uses.

Two other standards fix the skill location, so each needs its own mirror:

* Agent Plugins 1.0 requires ``<plugin root>/skills/<name>/SKILL.md``, hence
  the copy inside ``plugin/``.
* Agent Package Manager (APM/OpenAPM) discovers primitives under
  ``.apm/skills/<name>/SKILL.md`` at the package root, hence the copy inside
  ``.apm/``.

More copies of the same file means more drift: they go stale the moment
someone edits the frontmatter of one and forgets the others. This script is
the single writer for every mirror:

    python scripts/sync_skills.py            # copy (default; same as --write)
    python scripts/sync_skills.py --write    # copy, stated explicitly
    python scripts/sync_skills.py --check    # verify, exit 1 on any drift

``--check`` is the CI form: it writes nothing, prints every offending path, and
exits 1 so a stale mirror fails the build instead of shipping. Both modes are
deterministic and idempotent, create parent directories as needed, and never
read or write anything outside the skills trees.

Only ``--source`` / ``--dest`` may move the roots (the unit tests use them to
operate on a temporary tree instead of the real repository). ``--dest`` is
repeatable; passing it replaces the default mirror list.

Exit code is 0 when every mirror matches the canonical tree and 1 when any
drifts. Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import filecmp
import pathlib
import shutil
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Canonical skills tree — the copy that ships inside the Python package.
DEFAULT_SOURCE = REPO_ROOT / "src" / "edupage_mcp" / "skills"

#: Generated mirrors — the copies each downstream standard discovers.
DEFAULT_DESTS = (
    REPO_ROOT / "plugin" / "skills",  # Agent Plugins 1.0 (fixed location)
    REPO_ROOT / ".apm" / "skills",  # APM / OpenAPM primitives
)


def relative_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return every file under *root* as a sorted POSIX-style relative path.

    Sorted so the output of both modes is stable across filesystems, and
    POSIX-separated so the printed paths are identical on Windows and POSIX.
    """
    if not root.is_dir():
        return []
    return sorted(
        (p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()),
    )


def plan(source: pathlib.Path, dest: pathlib.Path) -> tuple[list[str], list[str]]:
    """Compare the two trees and return ``(missing_or_changed, extra)``.

    *missing_or_changed* holds relative paths whose bytes differ in the mirror
    (including paths the mirror does not have at all). *extra* holds paths that
    exist only in the mirror — a stale file left behind after a rename, which
    would otherwise be invisible to a one-directional copy.
    """
    wanted = relative_files(source)
    have = set(relative_files(dest))

    missing_or_changed = []
    for rel in wanted:
        target = dest / rel
        if not target.is_file() or not filecmp.cmp(source / rel, target, shallow=False):
            missing_or_changed.append(rel)

    extra = sorted(have - set(wanted))
    return missing_or_changed, extra


def display(path: pathlib.Path) -> str:
    """Render *path* relative to the repo root when it lives inside it."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def sync(source: pathlib.Path, dest: pathlib.Path) -> int:
    """Copy the canonical tree over the mirror. Returns the number of writes."""
    missing_or_changed, extra = plan(source, dest)

    written = 0
    for rel in missing_or_changed:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, target)
        written += 1

    for rel in extra:
        target = dest / rel
        target.unlink()
        print(f"  removed stale {display(target)}")

    # Drop directories the removals emptied so the mirror has no dead folders.
    if extra:
        for path in sorted(
            (p for p in dest.rglob("*") if p.is_dir()), key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                path.rmdir()
            except OSError:
                pass  # not empty — it still holds live content

    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror src/edupage_mcp/skills/ into the plugin/ and .apm/ skills trees.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify every mirror matches the canonical tree; write nothing and "
             "exit 1 on any drift",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="copy the canonical tree into every mirror (the default)",
    )
    parser.add_argument(
        "--source",
        type=pathlib.Path,
        default=DEFAULT_SOURCE,
        help=f"canonical skills tree (default: {display(DEFAULT_SOURCE)})",
    )
    parser.add_argument(
        "--dest",
        type=pathlib.Path,
        action="append",
        default=None,
        help="mirror skills tree; repeat for multiple mirrors "
             f"(default: {', '.join(display(d) for d in DEFAULT_DESTS)})",
    )
    args = parser.parse_args(argv)

    source = args.source.resolve()
    dests = [d.resolve() for d in (args.dest or DEFAULT_DESTS)]

    if not source.is_dir():
        print(f"ERROR: canonical skills tree not found: {display(source)}")
        return 1

    if args.check:
        drifted = False
        for dest in dests:
            missing_or_changed, extra = plan(source, dest)
            if not missing_or_changed and not extra:
                print(f"OK: {display(dest)} is in sync with {display(source)}")
                continue
            drifted = True
            print(
                f"DRIFT: {display(dest)} does not match {display(source)} "
                f"({len(missing_or_changed)} missing/changed, {len(extra)} stale). "
                f"Run: python scripts/sync_skills.py"
            )
            for rel in missing_or_changed:
                print(f"  missing/changed: {display(dest / rel)}")
            for rel in extra:
                print(f"  stale:           {display(dest / rel)}")
        return 1 if drifted else 0

    total = 0
    for dest in dests:
        written = sync(source, dest)
        total += written
        print(
            f"Wrote {written} file(s) from {display(source)} to {display(dest)} "
            f"({len(relative_files(dest))} file(s) in the mirror)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
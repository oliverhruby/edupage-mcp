"""Unit tests for the tool docstring contract (AGENTS.md rule 4).

Rule 4 says every tool docstring must carry a Google-style `Args:` block.
`test_tool_annotations.py` covers the `TOOL_ANNOTATIONS` registry; this file
covers the docstring side of the same rule.

The gap this closes: the 19 annotation tests never looked at `Args:`, so a
tool could ship with no `Args:` block and the suite stayed green. Three
tools did exactly that (`get_day_summary`, `scan_students`, and
`get_subdomains`, whose block read "(none)").

Caveat worth knowing before changing these tests: as of `mcp` 1.30.0
`FastMCP` has no `parse_docstrings` option and `griffe` is not a
dependency, so the `Args:` block is docstring *text* only - it is NOT
converted into per-parameter `description` entries in the advertised
`inputSchema`. A `tools/list` of all 31 tools currently exposes zero
parameter descriptions. This file therefore enforces the docstring
convention the repo documents, not a wire-format guarantee.
"""

from __future__ import annotations

import re
from pathlib import Path

import edupage_mcp

SRC = Path(edupage_mcp.__file__).read_text(encoding="utf-8")
LINES = SRC.split("\n")

# Section headers that terminate an Args: block inside a Google-style docstring.
_SECTION_RE = re.compile(r"^\s*(Args|Returns|Notes|Raises|Yields|Examples|Attributes):\s*$")
_ARGS_RE = re.compile(r"^\s*Args:\s*$")
_SIG_RE = re.compile(r"(?:async )?def ([a-z_0-9]+)\(")


def _tool_spans() -> list[tuple[str, int, int]]:
    """(name, def_line, end_line) for every @_tool function, in source order."""
    spans = []
    for i, line in enumerate(LINES):
        if line.strip() != "@_tool":
            continue
        for j in range(i + 1, min(i + 6, len(LINES))):
            m = _SIG_RE.match(LINES[j].strip())
            if m:
                # The body ends where the next top-level @_tool or decorator starts.
                end = len(LINES)
                for k in range(j + 1, len(LINES)):
                    if LINES[k].startswith("@") and LINES[k].strip():
                        end = k
                        break
                spans.append((m.group(1), j, end))
                break
    return spans


TOOL_SPANS = _tool_spans()
TOOLS = [name for name, _, _ in TOOL_SPANS]


def _docstring_lines(start: int, end: int) -> list[str]:
    """The docstring body of the tool whose `def` is at `start`."""
    for k in range(start + 1, min(start + 15, end)):
        if LINES[k].strip().startswith('"""'):
            return LINES[k + 1 : end]
    return []


def _signature_params(start: int) -> list[str]:
    """Parameter names of the tool whose `def` begins at `start`.

    The signature may wrap across lines, so join until the parens balance.
    """
    buf = []
    depth = 0
    for k in range(start, len(LINES)):
        buf.append(LINES[k])
        depth += LINES[k].count("(") - LINES[k].count(")")
        if depth <= 0 and "(" in "".join(buf):
            break
    sig = " ".join(" ".join(buf).split())
    inner = sig[sig.index("(") + 1 : sig.rindex(")")]
    if not inner.strip():
        return []
    params = []
    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue
        # Strip a default value and any annotation.
        name = part.split(":")[0].split("=")[0].strip()
        if name in ("*", "/"):
            continue
        params.append(name.lstrip("*"))
    return params


def _args_block(start: int, end: int) -> list[str] | None:
    """Lines of the Args: block, or None when the tool has none."""
    doc = _docstring_lines(start, end)
    try:
        i = next(idx for idx, line in enumerate(doc) if _ARGS_RE.match(line))
    except StopIteration:
        return None
    block = []
    for line in doc[i + 1 :]:
        if _SECTION_RE.match(line):
            break
        block.append(line)
    return block


def test_tools_are_discovered():
    # Guards against the parser silently returning [] and making every
    # coverage test below vacuously pass.
    assert len(TOOLS) == 31
    assert "get_day_summary" in TOOLS and "scan_students" in TOOLS


def test_every_tool_has_an_args_block():
    missing = [name for name, s, e in TOOL_SPANS if _args_block(s, e) is None]
    assert missing == []


def test_every_parameter_is_documented():
    """Each parameter needs its own entry in the Args: block.

    Keeps the docstring a complete contract for the tool's signature, so a
    parameter cannot be added without explaining what it does or how it
    interacts with the others.
    """
    undocumented = []
    for name, start, end in TOOL_SPANS:
        block = _args_block(start, end)
        if block is None:
            continue
        for param in _signature_params(start):
            if not any(re.match(r"\s*%s\s*:" % re.escape(param), line) for line in block):
                undocumented.append(f"{name}.{param}")
    assert undocumented == []


def test_zero_param_tools_still_declare_args():
    """No-arg tools must say so explicitly rather than omit the section.

    An omitted Args: block is indistinguishable from an oversight; an
    explicit "None." keeps the intent on the record.
    """
    for name, start, end in TOOL_SPANS:
        if _signature_params(start):
            continue
        block = _args_block(start, end)
        assert block is not None, f"{name} has no Args: block"
        assert any(re.search(r"\bNone\b", line) for line in block), (
            f"{name} takes no arguments; its Args: block should say so"
        )

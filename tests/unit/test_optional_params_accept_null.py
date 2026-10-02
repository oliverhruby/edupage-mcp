"""Regression guard: every optional tool parameter must accept an explicit null.

Background. EduPage MCP parameters are declared like `subdomain: str = None`.
Pydantic renders that as a schema of `{"type": "string", "default": null}` --
which advertises null as the default while *forbidding* null as a value. Any
client that builds its payload from the schema and sends explicit nulls for
unset optional fields (n8n does exactly this; so do several LLM tool-calling
clients) gets a hard failure before the tool body ever runs:

    1 validation error for get_my_studentsArguments
    subdomain
      Input should be a valid string [type=string_type, input_value=None]

The fix is `Optional[str]`, which renders `anyOf: [{string}, {null}]`. These
tests assert the *rendered schema*, not the annotations, because the schema is
what the client actually consumes -- a correct-looking annotation that renders
the wrong schema would still break users.
"""

import asyncio

import pytest

import edupage_mcp

# Types that reject null when a schema pins them to a single, non-null type.
SINGLE_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


def _tools() -> dict:
    async def _load():
        return {t.name: t for t in await edupage_mcp.server.list_tools()}

    return asyncio.run(_load())


TOOLS = _tools()


def _optional_props(tool) -> list[tuple[str, dict]]:
    schema = tool.inputSchema or {}
    required = schema.get("required") or []
    return [
        (name, spec)
        for name, spec in (schema.get("properties") or {}).items()
        if name not in required
    ]


def test_tool_surface_is_non_trivial():
    """Guard against the loader silently returning nothing."""
    assert len(TOOLS) >= 30


@pytest.mark.parametrize("tool_name", sorted(TOOLS))
def test_optional_params_accept_null(tool_name):
    """No optional parameter may pin a non-nullable type."""
    offenders = [
        f"{tool_name}.{pname} (type={spec.get('type')!r})"
        for pname, spec in _optional_props(TOOLS[tool_name])
        if spec.get("type") in SINGLE_TYPES
    ]
    assert not offenders, (
        "Optional parameters that reject an explicit null: " + ", ".join(offenders)
    )


@pytest.mark.parametrize("tool_name", sorted(TOOLS))
def test_required_params_are_actually_required(tool_name):
    """A required param must not carry a default the client can never send."""
    schema = TOOLS[tool_name].inputSchema or {}
    for pname in schema.get("required") or []:
        spec = (schema.get("properties") or {}).get(pname) or {}
        assert "default" not in spec or spec["default"] is not None, (
            f"{tool_name}.{pname} is required but defaults to null"
        )


def test_subdomain_null_is_accepted_end_to_end():
    """Regression test for the exact n8n failure report.

    `get_my_students(subdomain=None)` must behave like omitting the argument:
    resolve the active subdomain rather than raising a validation error.
    """
    async def _call():
        return await edupage_mcp.server.call_tool(
            "get_my_students", {"subdomain": None}
        )

    result = asyncio.run(_call())
    text = result[1][0].text if isinstance(result, tuple) else str(result)
    # With no EduPage client configured the call legitimately fails inside the
    # tool. What must NOT happen is a schema validation error -- the original
    # bug rejected null before the tool body ran, so a login error (or any
    # runtime error) proves validation now passes.
    assert "validation error" not in text.lower(), (
        f"get_my_students(null) was rejected by the schema: {text}"
    )
    assert "subdomain\n" not in text, (
        f"get_my_students(null) failed argument validation: {text}"
    )


def test_timeline_null_category_uses_default():
    """`category=None` must mean the default 'recent', not an unknown category.

    `get_timeline` returns an error payload rather than raising, so a null
    category would otherwise pass validation and then fail inside the tool.
    """
    result = edupage_mcp.get_timeline(category=None, subdomain="nonexistent-school")
    # No client means the call cannot succeed, but it must fail for the *right*
    # reason -- a missing session, never a category-resolution error.
    assert "unknown category" not in result.get("error", "").lower()


# Minimal real values for params the schema marks genuinely required. Anything
# optional is deliberately left to be sent as null, which is the whole point.
REQUIRED_VALUES = {
    "get_roster": {"roster_type": "teachers"},
    "find_student": {"name": "x"},
    "custom_request": {"url": "/", "method": "GET"},
    "get_timetable": {"target_type": "class", "target_id": "1"},
    "rate_meal": {"date_str": "2026-01-01", "meal_type": "lunch",
                  "quality": 3, "quantity": 3},
    "choose_meal": {"date_str": "2026-01-01", "meal_type": "lunch", "number": 1},
    "sign_off_meal": {"date_str": "2026-01-01", "meal_type": "lunch"},
    "send_message": {"recipient_id": "Teacher1", "body": "hi"},
    "download_attachment": {"url": "https://example.invalid/y"},
    "get_homework_material": {"superid": "1"},
}


@pytest.mark.parametrize("tool_name", sorted(TOOLS))
def test_tool_accepts_all_null_payload(tool_name):
    """The exact payload shape n8n builds: every property present, unset
    optionals explicitly null, required params given a real value.

    Asserted end to end through the MCP call path, because a schema that merely
    *looks* nullable is not proof that validation actually passes.
    """
    tool = TOOLS[tool_name]
    schema = tool.inputSchema or {}
    declared_required = set(schema.get("required") or [])
    fixtures = REQUIRED_VALUES.get(tool_name, {})
    assert declared_required.issubset(fixtures), (
        f"{tool_name} requires {sorted(declared_required - fixtures)}; add a "
        f"fixture so the all-null payload is a legitimate call"
    )

    payload = {name: None for name in (schema.get("properties") or {})}
    payload.update(fixtures)

    async def _call():
        return await edupage_mcp.server.call_tool(tool_name, payload)

    try:
        result = asyncio.run(_call())
        text = result[1][0].text if isinstance(result, tuple) else str(result)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
    assert "validation error" not in text.lower(), (
        f"{tool_name} rejected a valid payload shape: {text[:200]}"
    )
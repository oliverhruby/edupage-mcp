"""edupage MCP server.

Exposes the full feature set of the `edupage-api` Python library as MCP tools:
login (standard, auto, session-id, 2FA), timetables, school year, ringing,
grades, notifications/history (homework, exams...), substitutions, missing
teachers, meals (+ ordering), rosters (students/teachers/classes/classrooms/
subjects), messages, role-aware student switching, multi-school auto-discovery,
and custom requests.

Careful: login/send_message/switch_to_student/meal actions mutate Edupage state.
All `get_*` tools are read-only.
"""

import builtins
import datetime as _dt
import html
import json
import os
import re
import sys
import time
import unicodedata
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from urllib.parse import urlparse
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time as dt_time
from enum import Enum
from types import SimpleNamespace

from edupage_api import Edupage
from edupage_api import exceptions as edupage_exceptions
from edupage_api.classes import Class
from edupage_api.classrooms import Classroom
from edupage_api.grades import EduGrade, Term
from edupage_api.lunches import Meal, MealType, Menu, Meals, Rating
from edupage_api.people import EduAccount, EduParent, EduStudent, EduStudentSkeleton, EduTeacher
from edupage_api.ringing import RingingTime, RingingType
from edupage_api.subjects import Subject
from edupage_api.substitution import Action, TimetableChange
from edupage_api.timeline import TimelineEvents as _TimelineEvents
from edupage_api.timeline import EventType, TimelineEvent
from edupage_api.timetables import Lesson, Timetable

# ---------------------------------------------------------------------------
# Local workaround for an upstream edupage-api bug (master as of 0.12.5):
# get_notifications_history() passes data["timelineUserProps"] straight to
# __parse_items, but for some schools (e.g. iprskola) that field arrives as a
# JSON array ([]) instead of a dict, so __parse_items crashes with
# "'list' object has no attribute 'get'". Coerce any non-dict user_props to {}
# at this boundary. Revisit once upstream normalizes this shape itself.
# ---------------------------------------------------------------------------
_UPSTREAM_PARSE_ITEMS = getattr(_TimelineEvents, "_TimelineEvents__parse_items")


def _parse_items_coerce_user_props(self, timeline_items, user_props=None):
    if not isinstance(user_props, dict):
        user_props = {}
    return _UPSTREAM_PARSE_ITEMS(self, timeline_items, user_props)


_TimelineEvents._TimelineEvents__parse_items = _parse_items_coerce_user_props

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.server.auth.settings import AuthSettings
except Exception:
    FastMCP = None
    AccessToken = None
    TokenVerifier = None
    AuthSettings = None

EDUPAGE_USERNAME = os.environ.get("EDUPAGE_USERNAME", "")
EDUPAGE_PASSWORD = os.environ.get("EDUPAGE_PASSWORD", "")
# Comma-separated list of schools to auto-login on startup (multi-school + automatic
# student discovery across all of them).
EDUPAGE_SUBDOMAINS = os.environ.get("EDUPAGE_SUBDOMAINS", "")
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
_MCP_PORT_RAW = os.environ.get("MCP_PORT", "8000")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
_MCP_PORT_ERROR = None
try:
    MCP_PORT = int(_MCP_PORT_RAW)
    if MCP_PORT <= 0 or MCP_PORT > 65535:
        raise ValueError
except ValueError:
    MCP_PORT = 8000
    _MCP_PORT_ERROR = f"Error: invalid MCP_PORT '{_MCP_PORT_RAW}'. Expected an integer between 1 and 65535."

# Multiple-school support: one Edupage() session per subdomain.
_clients = {}          # subdomain -> Edupage
_two_factor = {}       # subdomain -> TwoFactorLogin
_active_subdomain = None
_roles = {}            # subdomain -> "student" | "parent" | "teacher"

# Student data cache to avoid redundant API calls across tools.
# Keyed by (subdomain, role) -> {"students": [...], "timestamp": float}
_student_cache = {}
_STUDENT_CACHE_TTL = 300  # 5 minutes

# Track auto-login failures (2FA, network, etc.) per subdomain.
_autologin_failures = {}  # subdomain -> error message


def _first_subdomain_from_env():
    subs = [s.strip() for s in EDUPAGE_SUBDOMAINS.split(",") if s.strip()]
    return subs[0] if subs else None


def _configured_subdomains():
    """Subdomains listed in EDUPAGE_SUBDOMAINS (empty list when unset)."""
    return [s.strip() for s in EDUPAGE_SUBDOMAINS.split(",") if s.strip()]


def _subdomain_is_configured(sub):
    """True when `sub` is a legitimate target for this server instance.

    With EDUPAGE_SUBDOMAINS empty (auto-discovery mode) every subdomain is
    allowed. When it is set, only the listed schools are expected targets; any
    other subdomain is a configuration error — it must never be presented as a
    login candidate, used as an auto-relogin target, or logged in implicitly."""
    configured = _configured_subdomains()
    return (not configured) or (sub in configured)


def _discovery_subdomains():
    """Subdomains available to discovery/scanning tools.

    Restricted to the schools listed in `EDUPAGE_SUBDOMAINS` (comma-separated)
    whenever that variable is set, so auto-discovery never touches a school the
    user did not opt in to via SUBDOMAINS. When it is not set, every session
    in `_clients` is used. Callers must still surface login blocks for
    configured-but-unavailable schools instead of silently skipping them."""
    configured = _configured_subdomains()
    if configured:
        return configured
    return [s for s, c in _clients.items() if c is not None]


def fail(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _resolve_subdomain(subdomain=None):
    if subdomain:
        return subdomain
    return _active_subdomain


def _login_block_message(sub):
    """Return a user-facing reason why `sub` cannot be queried, or None if usable.

    A session is blocked when there is no logged-in client, a startup/login
    failure was recorded for the subdomain, or 2FA was never finished. Callers
    must surface this as a login prompt — never treat a session problem as an
    empty result list."""
    if not sub:
        return None
    client = _clients.get(sub)
    if client is None or not getattr(client, "is_logged_in", False):
        if not _subdomain_is_configured(sub):
            configured = _configured_subdomains()
            scope = ", ".join(configured) if configured else "<unset>"
            return (f"Subdomain '{sub}' is not configured in EDUPAGE_SUBDOMAINS "
                    f"(current scope: {scope}). Add it to the configured list and "
                    f"restart the server, or leave EDUPAGE_SUBDOMAINS empty for "
                    f"auto-discovery. No login can be attempted for an unconfigured school.")
        msg = f"Not logged in for subdomain '{sub}'. Call `login` (or `login_all` for multiple schools) first."
        failure = _autologin_failures.get(sub)
        if failure:
            msg = (f"Login failed for subdomain '{sub}': {failure}. "
                   f"Call `login_all` (or `login`) to retry before querying data.")
        return msg
    failure = _autologin_failures.get(sub)
    if failure:
        return (f"Login for subdomain '{sub}' failed earlier ({failure}); the session may be "
                f"unreliable. Re-run `login`/`login_all` to refresh it, then retry.")
    return None


def _require_client(subdomain=None):
    sub = _resolve_subdomain(subdomain)
    block = _login_block_message(sub)
    if block:
        raise RuntimeError(block)
    return _clients[sub]


def _resolve_role(client):
    """Return 'student', 'parent', or 'teacher' for the logged-in account."""
    uid = client.get_user_id() or ""
    if "Rodic" in uid:
        return "parent"
    if "Teacher" in uid:
        return "teacher"
    return "student"


def _student_name(student):
    name = getattr(student, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    short = getattr(student, "name_short", None)
    if isinstance(short, str) and short.strip():
        return short.strip()
    return ""


def _resolve_student_full_name(client, subdomain, student):
    """Return the best available full name for a student.
    If student has full name, use it. If only short name (initials),
    search recent timeline notifications for that student_id to find full name."""
    full = _student_name(student)
    # Check if it's likely a full name (has space, > 2 parts, not just initials)
    if full and not _looks_like_initials(full):
        return full
    # Only short name/initials - try to enrich from notifications
    sid = getattr(student, "person_id", None)
    if sid is None:
        return full
    try:
        events = client.get_notifications() or []
        for e in events:
            recipient = getattr(e, "recipient", "") or ""
            ad = getattr(e, "additional_data", None)
            # Match by student_id in recipient or additional_data
            sid_str = str(sid)
            matched = False
            if sid_str in recipient:
                matched = True
            elif ad and isinstance(ad, dict):
                for v in ad.values():
                    if sid_str in str(v):
                        matched = True
                        break
            elif ad and isinstance(ad, list):
                for v in ad:
                    if sid_str in str(v):
                        matched = True
                        break
            if matched:
                # Try to extract name from recipient
                if recipient and not _looks_like_initials(recipient):
                    return recipient.strip()
    except Exception:
        pass
    return full


def _looks_like_initials(text):
    """Check if text looks like initials/short name (e.g. 'TH', 'V.H.', 'JH')."""
    if not text:
        return True
    t = text.strip()
    # All caps, 1-3 chars, possibly with dots
    if len(t) <= 3 and t.upper() == t and t.replace(".", "").isalpha():
        return True
    # Pattern like "V.H." or "V H"
    if re.match(r"^[A-Z]\.?\s*[A-Z]?\.?$", t):
        return True
    return False


def _normalize_text(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _has_full_name(student):
    name = _student_name(student)
    if not name:
        return False
    parts = [p for p in name.replace(",", " ").split() if p]
    return len(parts) >= 2 and any(len(p) > 2 for p in parts)


def _relogin_subdomain(subdomain):
    user = EDUPAGE_USERNAME
    pwd = EDUPAGE_PASSWORD
    if not (subdomain and user and pwd):
        return False
    if not _subdomain_is_configured(subdomain):
        _autologin_failures[subdomain] = (
            f"subdomain not in EDUPAGE_SUBDOMAINS scope "
            f"({', '.join(_configured_subdomains()) or '<unset>'})"
        )
        return False
    client = Edupage()
    tf = client.login(user, pwd, subdomain)
    _clients[subdomain] = client
    _two_factor[subdomain] = tf
    _roles[subdomain] = _resolve_role(client)
    return True


# EduPage renders a parent's linked children into the school homepage:
# * ASC.req_props.parent_studentid holds the currently selected child,
# * `.switchChildBtn` anchors carry every child (data-sid + .userName span).
_PAT_PARENT_STUDENTID = re.compile(r"""parent_studentid["\s\\]*:\s*["\s\\]*(-?\d+)""")
_PAT_SWITCH_CHILD_BTN = re.compile(
    r"""<a[^>]*class="[^"]*switchChildBtn[^"]*"[^>]*data-sid="(-?\d+)"[^>]*>(.*?)</a>""",
    re.DOTALL,
)
_PAT_CHILD_NAME = re.compile(
    r"""<span[^>]*class="[^"]*userName[^"]*"[^>]*>(.*?)</span>""",
    re.DOTALL,
)


def _get_parent_children(client, subdomain):
    """Discover a parent account's linked children by parsing the school homepage.

    EduPage does not expose children through the edupage-api roster methods;
    it renders them server-side: the currently selected child in
    ASC.req_props.parent_studentid and every linked child as a
    `.switchChildBtn` anchor with a data-sid and a `.userName` span.

    Returns a list of SimpleNamespace objects with .person_id, .name and
    .class_id (None). Raises RuntimeError when the homepage cannot be fetched
    (not logged in, HTTP error, captcha) so callers never mistake a session
    problem for 'no children'.
    """
    if not (client and getattr(client, "is_logged_in", False)):
        raise RuntimeError(
            f"Session for '{subdomain}' is not logged in (login failed or expired). "
            "Re-run `login`/`login_all` before querying data."
        )
    url = f"https://{subdomain}.edupage.org/"
    try:
        resp = client.session.get(url)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"Could not fetch homepage of '{subdomain}' to discover children: "
            f"{type(e).__name__}: {e}"
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Homepage of '{subdomain}' returned HTTP {resp.status_code}; children "
            "cannot be discovered. The session may have expired — re-run `login`."
        )
    page = resp.text or ""
    if "captcha" in page.lower():
        raise RuntimeError(
            f"'{subdomain}' is captcha-challenged; children cannot be discovered. "
            "Wait a while, then re-run `login`/`login_all`."
        )

    children = {}
    for m in _PAT_SWITCH_CHILD_BTN.finditer(page):
        sid = int(m.group(1))
        name = ""
        name_m = _PAT_CHILD_NAME.search(m.group(2))
        if name_m:
            name = html.unescape(re.sub(r"<[^>]+>", "", name_m.group(1))).strip()
            # Display name looks like "Surname Name, ClassCode" - drop the class suffix.
            name = re.sub(r",\s*[^,]+$", "", name).rstrip()
        children.setdefault(sid, SimpleNamespace(person_id=sid, name=name, class_id=None))
    psid_m = _PAT_PARENT_STUDENTID.search(page)
    if psid_m:
        sid = int(psid_m.group(1))
        children.setdefault(sid, SimpleNamespace(person_id=sid, name="", class_id=None))
    for child in children.values():
        if not child.name:
            child.name = str(child.person_id)
    return list(children.values())


def _get_students_cached(client, subdomain):
    """Get students for a school, using cache to avoid redundant API calls.
    Returns list of student-like objects with .person_id, .name, .class_id.
    For parent accounts: the parent's actual children (parsed from the school
    homepage). A failed homepage fetch raises — it is never turned into 'no
    children', and there is no roster fallback (a parent's school-wide roster is
    not the same as their linked children)."""
    role = _roles.get(subdomain, "student")
    cache_key = (subdomain, role)
    now = time.time()
    cached = _student_cache.get(cache_key)
    if cached and (now - cached["timestamp"]) < _STUDENT_CACHE_TTL:
        return cached["students"]
    if role == "parent":
        students = _get_parent_children(client, subdomain)
    else:
        students = client.get_students() or []
    _student_cache[cache_key] = {"students": students, "timestamp": now}
    return students


def _drop_student_cache(subdomain):
    """Drop cached student data for one subdomain (re-login invalidates it)."""
    for key in [k for k in list(_student_cache.keys()) if k[0] == subdomain]:
        del _student_cache[key]
    _autologin_failures.pop(subdomain, None)


def _humanize(value):
    if isinstance(value, Enum):
        return value.value
    if value is None:
        return None
    return str(value)


def _serialize(obj):
    """Convert edupage-api objects (dataclasses, enums, times, dicts) to plain data."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime, date, dt_time)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_serialize(v) for v in obj]
    if is_dataclass(obj):
        out = {}
        for f in fields(obj):
            if f.name.startswith("__"):
                continue
            out[f.name] = _serialize(getattr(obj, f.name))
        return out
    if hasattr(obj, "__dict__"):
        out = {}
        for k, v in vars(obj).items():
            if k.startswith("_"):
                continue
            out[k] = _serialize(v)
        return out
    return _humanize(obj)


def _to_text(data) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(_serialize(data), ensure_ascii=False, indent=2)}]}


# --------------------------------------------------------------------------
# helper indirection so FastMCP is optional (tests can call these directly)
# --------------------------------------------------------------------------
class _StaticApiKeyTokenVerifier:
    """Simple bearer token verifier backed by MCP_API_KEY."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def verify_token(self, token: str):
        if token != self.api_key:
            return None
        return AccessToken(token=token, client_id="mcp-api-key", scopes=["mcp"])  # type: ignore[misc]


_APP_DIST = "edupage-mcp-full"


def _server_version() -> str:
    """Our published package version, from the installed distribution metadata.

    Single source of truth is `version` in pyproject.toml. Both PyPI wheels and
    the Docker image install this package via `pip`, so importlib.metadata
    resolves the same number everywhere. Falls back to "dev" only for bare
    source checkouts (not pip-installed)."""
    try:
        return _pkg_version(_APP_DIST)
    except PackageNotFoundError:
        return "dev"


if FastMCP:
    token_verifier = None
    auth_settings = None
    if MCP_API_KEY:
        token_verifier = _StaticApiKeyTokenVerifier(MCP_API_KEY)
        # FastMCP requires explicit auth settings whenever a token verifier is used.
        auth_settings = AuthSettings(
            issuer_url=f"http://{MCP_HOST}:{MCP_PORT}",
            resource_server_url=f"http://{MCP_HOST}:{MCP_PORT}",
            required_scopes=["mcp"],
        )
    server = FastMCP(
        "edupage",
        host=MCP_HOST,
        port=MCP_PORT,
        auth=auth_settings,
        token_verifier=token_verifier,
    )
    # FastMCP 1.x exposes no `version` parameter and the lowlevel Server falls
    # back to pkg_version("mcp"), so `initialize` would report the *mcp SDK*
    # version instead of ours. Pin it to our own distribution version so the
    # serverInfo advertises the version we published on PyPI / as a container.
    server._mcp_server.version = _server_version()
else:
    server = None


def _tool(fn):
    if server is not None:
        return server.tool()(fn)
    return fn


def _run(fn, error_label="edupage call"):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return fail(f"{error_label} failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# Login / session
# --------------------------------------------------------------------------
@_tool
def login(
    username: str = None,
    password: str = None,
    subdomain: str = None,
    method: str = "credentials",
    session_id: str = None,
) -> dict:
    """Log in to Edupage for a school. Writes: establishes (or replaces) the
    server-side session for that subdomain.

    Args:
        username: EduPage account login. Falls back to EDUPAGE_USERNAME.
        password: Account password. Falls back to EDUPAGE_PASSWORD.
        subdomain: School subdomain (e.g. 'school'). Falls back to the first
            value of EDUPAGE_SUBDOMAINS; for method='auto' it is optional and
            tags the detected school's session.
        method: How to authenticate:
            - 'credentials' (default) — username/password for a known subdomain.
            - 'auto' — portal auto-detect of the school (formerly `login_auto`).
            - 'session' — build a session from an existing PHPSESSID cookie
              (formerly `login_from_session`); pass it in `session_id`.
        session_id: PHPSESSID cookie value, required when method='session'.

    Returns:
        dict with logged_in status, subdomain, user_id, role and whether 2FA
        is pending. When `two_factor_required` is true, finish with
        `two_factor_finish`.

    Notes:
        - Each `login` call adds/replaces that subdomain's session; call
          `login_all` to log into several schools in one call.
        - Prefer setting EDUPAGE_USERNAME / EDUPAGE_PASSWORD (and
          EDUPAGE_SUBDOMAINS for multi-school) — the server then logs in
          automatically at startup.
    """
    global _clients, _two_factor, _active_subdomain

    def go():
        global _clients, _two_factor, _active_subdomain
        user = username or EDUPAGE_USERNAME
        pwd = password or EDUPAGE_PASSWORD
        if method == "session":
            if not (session_id and subdomain and username):
                raise RuntimeError(
                    "method='session' requires session_id, subdomain and username."
                )
            client = Edupage.from_session_id(session_id, subdomain, username)
            sub = subdomain
            tf = None
        elif method == "auto":
            if not (user and pwd):
                raise RuntimeError(
                    "method='auto' requires username and password (or env vars)."
                )
            client = Edupage()
            try:
                tf = client.login_auto(user, pwd)
            except Exception as e:  # noqa: BLE001
                _autologin_failures[subdomain or "portal"] = f"{type(e).__name__}: {e}"
                raise
            sub = subdomain or client.subdomain or "auto"
        elif method == "credentials":
            sub = subdomain or _first_subdomain_from_env()
            if not (user and pwd and sub):
                raise RuntimeError(
                    "username, password and subdomain must be provided (or set as env vars)."
                )
            client = Edupage()
            try:
                tf = client.login(user, pwd, sub)
            except Exception as e:  # noqa: BLE001
                _autologin_failures[sub] = f"{type(e).__name__}: {e}"
                raise
        else:
            raise RuntimeError("method must be 'credentials', 'auto' or 'session'.")
        _drop_student_cache(sub)
        _clients[sub] = client
        _two_factor[sub] = tf
        _roles[sub] = _resolve_role(client)
        _active_subdomain = sub
        return {
            "logged_in": True,
            "username": user,
            "subdomain": sub,
            "user_id": client.get_user_id(),
            "role": _roles[sub],
            "two_factor_required": tf is not None,
        }

    return _run(go, "login")


@_tool
def login_all(subdomains: str = None, usernames: str = None, passwords: str = None) -> dict:
    """Log in to one or more schools in a single call. Writes: establishes
    (or replaces) the server-side session for each subdomain.

    Args:
        subdomains: Comma-separated school subdomains,
            e.g. 'school1,school2'. Falls back to EDUPAGE_SUBDOMAINS.
        usernames: Comma-separated usernames, one per school (or a single one).
            Falls back to EDUPAGE_USERNAME.
        passwords: Comma-separated passwords, one per school (or a single one).
            Falls back to EDUPAGE_PASSWORD.

    Returns:
        dict: {'results': [{subdomain, ok, user_id, role,
        two_factor_required}], 'active_subdomain': ...}. A failed school is
        reported per-entry with its error.

    Notes:
        - Add schools one at a time with `login`; finish any pending 2FA with
          `two_factor_finish`.
    """
    global _clients, _two_factor, _active_subdomain

    def go():
        global _clients, _two_factor, _active_subdomain, _roles
        subs = [s.strip() for s in (subdomains or EDUPAGE_SUBDOMAINS).split(",") if s.strip()]
        users = [u.strip() for u in (usernames or EDUPAGE_USERNAME).split(",") if u.strip()] or [EDUPAGE_USERNAME]
        pwds = [p.strip() for p in (passwords or EDUPAGE_PASSWORD).split(",") if p.strip()] or [EDUPAGE_PASSWORD]
        results = []
        for i, sub in enumerate(subs):
            user = users[i] if i < len(users) else users[-1]
            pwd = pwds[i] if i < len(pwds) else pwds[-1]
            if not (user and pwd):
                results.append({"subdomain": sub, "ok": False, "error": "missing credentials"})
                continue
            try:
                client = Edupage()
                tf = client.login(user, pwd, sub)
                _drop_student_cache(sub)
                _clients[sub] = client
                _two_factor[sub] = tf
                _roles[sub] = _resolve_role(client)
                results.append({"subdomain": sub, "ok": True,
                                "user_id": client.get_user_id(),
                                "role": _roles[sub],
                                "two_factor_required": tf is not None})
            except Exception as e:  # noqa: BLE001
                _autologin_failures[sub] = f"{type(e).__name__}: {e}"
                results.append({"subdomain": sub, "ok": False, "error": f"{type(e).__name__}: {e}"})
        if _clients:
            _active_subdomain = _clients and next(iter(_clients))
        return {"results": results, "active_subdomain": _active_subdomain}

    return _run(go, "login_all")


@_tool
def two_factor_finish(
    code: str = None, subdomain: str = None, poll_seconds: int = 60
) -> dict:
    """Finish a pending 2FA login. Writes: completes the pending auth flow.
    Only needed after a `login` that returned `two_factor_required: True`.

    Args:
        code: Optional email/app verification code. When given it is used
            directly; otherwise the device-confirmation flow is polled.
        subdomain: School subdomain whose pending login to complete (defaults
            to the active subdomain).
        poll_seconds: How long (seconds) to wait for approval on a device when
            `code` is not given. Defaults to 60; on timeout the caller can
            call `two_factor_finish` again later.

    Returns:
        dict: {'confirmed': True, 'logged_in': True, subdomain, user_id, role}
        on success, or a pending status when the confirmation wasn't approved
        within the poll window.
    """
    global _two_factor, _roles

    def go():
        global _two_factor, _roles
        sub = _resolve_subdomain(subdomain)
        client = _require_client(sub)
        tf = _two_factor.get(sub)
        if tf is None:
            raise RuntimeError(f"No pending 2FA login for '{sub}'. Call `login` first.")
        if code:
            tf.finish_with_code(code)
        else:
            deadline = time.monotonic() + max(0, int(poll_seconds))
            while not tf.is_confirmed() and time.monotonic() < deadline:
                time.sleep(1)
            if not tf.is_confirmed():
                return {
                    "confirmed": False,
                    "subdomain": sub,
                    "message": "Not approved on a device yet. Approve it, then call "
                    "`two_factor_finish` again (or pass a `code`).",
                }
            tf.finish()
        _two_factor[sub] = None
        _drop_student_cache(sub)
        _roles[sub] = _resolve_role(client)
        return {"confirmed": True, "logged_in": True, "subdomain": sub,
                "user_id": client.get_user_id(), "role": _roles[sub]}

    return _run(go, "two_factor finish")


@_tool
def get_school_year(subdomain: str = None) -> dict:
    """Return the current school year (starting year). Read-only.

    Args:
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'school_year': <int>, 'subdomain': ...}.
    """
    def go():
        client = _require_client(subdomain)
        return {"school_year": client.get_school_year(), "subdomain": _resolve_subdomain(subdomain)}
    return _run(go, "get_school_year")


# --------------------------------------------------------------------------
# Timetables
# --------------------------------------------------------------------------
def _parse_date(value):
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RuntimeError(f"Invalid date '{value}', expected YYYY-MM-DD.")


@_tool
def get_my_timetable(date_str: str = None, subdomain: str = None) -> dict:
    """Get the timetable for the logged-in user on a date. Read-only.

    Args:
        date_str: YYYY-MM-DD (default today).
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'date', 'subdomain', 'lessons': [serialized lessons]}.

    Notes:
        - For *another* student use `get_student_timetable` (by name/id);
          for a teacher/class/room on a date or a range use `get_timetable`.
        - Next week for yourself: `get_next_week_timetable`.
    """
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        try:
            tt = client.get_my_timetable(d)
        except (IndexError, KeyError, AttributeError, TypeError):
            tt = None
        if tt is None:
            return {"date": d.isoformat(), "subdomain": _resolve_subdomain(subdomain), "lessons": []}
        return {"date": d.isoformat(), "subdomain": _resolve_subdomain(subdomain),
                "lessons": [_serialize(ls) for ls in tt.lessons]}

    return _run(go, "get_my_timetable")


def _resolve_target(client, target_type, target_id):
    if target_type == "teacher":
        for t in client.get_teachers() or []:
            if str(t.person_id) == str(target_id):
                return t
        raise RuntimeError(f"Teacher {target_id} not found.")
    if target_type == "student":
        for s in client.get_students() or []:
            if str(s.person_id) == str(target_id):
                return s
        for s in client.get_all_students() or []:
            if str(getattr(s, "person_id", "")) == str(target_id):
                return s
        raise RuntimeError(f"Student {target_id} not found.")
    if target_type == "class":
        for c in client.get_classes() or []:
            if str(c.class_id) == str(target_id):
                return c
        raise RuntimeError(f"Class {target_id} not found.")
    if target_type == "classroom":
        for c in client.get_classrooms() or []:
            if str(c.classroom_id) == str(target_id):
                return c
        raise RuntimeError(f"Classroom {target_id} not found.")
    raise RuntimeError("target_type must be teacher, student, class or classroom.")


def _find_student(client, name: str, subdomain=None):
    """Find a student by name within a school using tiered matching.

    Matching tiers (highest confidence first):
      1. Exact full name match (case-insensitive)
      2. First name match (needle is a single word matching a first name)
      3. Last name match (needle matches a last name)
      4. Short name match for parent accounts (e.g. 'Doe J.' matches 'John Doe')

    Returns the best single match. Raises RuntimeError if 0 or >1 matches at the
    highest populated tier."""
    matches = _find_student_all(client, name, subdomain)
    if not matches:
        raise RuntimeError(f"No student named '{name}' found in this school.")
    if len(matches) == 1:
        return matches[0]["_raw"]
    # Multiple matches — find the highest-confidence tier among them
    best_tier = min(m["tier"] for m in matches)
    best = [m for m in matches if m["tier"] == best_tier]
    if len(best) == 1:
        return best[0]["_raw"]
    names = ", ".join(m["name"] for m in best)
    raise RuntimeError(
        f"Ambiguous: {len(best)} students match '{name}' in this school: {names}. "
        "Provide a more specific name or use student_id."
    )


def _find_student_all(client, name, subdomain=None):
    """Search for students matching `name` across all tiers. Returns list of
    dicts with keys: name, student_id, class_id, subdomain, tier, confidence, _raw."""
    needle = str(name).strip().lower()
    needle_norm = _normalize_text(name)
    if not needle:
        return []
    sub = subdomain or ""
    students = _get_students_cached(client, sub)
    results = []

    def safe_attr(obj, attr, default=None):
        try:
            value = getattr(obj, attr, default)
        except Exception:
            return default
        return default if value is None else value

    for s in students:
        sname = safe_attr(s, "name", "") or safe_attr(s, "name_short", "")
        # Build name parts: try full name, comma-separated, space-separated
        full_lower = sname.strip().lower()
        full_norm = _normalize_text(sname)
        parts = [p.strip().lower() for p in sname.replace(",", " ").split() if p.strip()]
        parts_norm = [_normalize_text(p) for p in sname.replace(",", " ").split() if p.strip()]
        # Also try the short name field if present (parent accounts)
        short = safe_attr(s, "name_short", "")
        short_lower = short.strip().lower()
        short_norm_text = _normalize_text(short)
        short_parts = [p.strip().lower() for p in short.replace(",", " ").split() if p.strip()]
        short_parts_norm = [_normalize_text(p) for p in short.replace(",", " ").split() if p.strip()]
        short_initials = "".join(ch for ch in short_norm_text if ch.isalnum())
        needle_parts = [p for p in needle.split() if p]
        needle_parts_norm = [p for p in needle_norm.split() if p]

        tier = None
        confidence = 0.0

        # Tier 1: Exact full name match
        if needle == full_lower or needle_norm == full_norm:
            tier = 1
            confidence = 1.0
        # Tier 1b: Exact short name match
        elif short_lower and (needle == short_lower or needle_norm == short_norm_text):
            tier = 1
            confidence = 0.95
        # Tier 2: First name match (needle is a single word)
        elif len(parts) >= 2 and (needle == parts[0] or needle_norm == parts_norm[0]):
            tier = 2
            confidence = 0.85
        elif short_parts and len(short_parts) >= 2 and (needle == short_parts[0] or needle_norm == short_parts_norm[0]):
            tier = 2
            confidence = 0.80
        # Tier 3: Last name match
        elif len(parts) >= 2 and (needle == parts[-1] or needle_norm == parts_norm[-1]):
            tier = 3
            confidence = 0.70
        elif short_parts and len(short_parts) >= 2 and (needle == short_parts[-1] or needle_norm == short_parts_norm[-1]):
            tier = 3
            confidence = 0.65
        # Tier 3b: initial-based short names (e.g. "John Doe" vs "JD")
        elif short_initials and len(needle_parts_norm) >= 2:
            initials = "".join(p[0] for p in needle_parts_norm if p)
            if initials and short_initials.startswith(initials):
                tier = 3
                confidence = 0.60
        # Tier 4: Substring match (last resort, lower confidence)
        elif (
            needle in full_lower
            or (short_lower and needle in short_lower)
            or (needle_norm and needle_norm in full_norm)
            or (short_norm_text and needle_norm and needle_norm in short_norm_text)
        ):
            tier = 4
            confidence = 0.40

        if tier is not None:
            results.append({
                "name": sname,
                "student_id": safe_attr(s, "person_id", None),
                "class_id": safe_attr(s, "class_id", None),
                "subdomain": sub,
                "tier": tier,
                "confidence": confidence,
                "_raw": s,
            })
    # Sort by tier (best first), then confidence
    results.sort(key=lambda r: (r["tier"], -r["confidence"]))
    return results


def _student_timetable_at(client, sub, name, student_id, d):
    """Resolve a student (by name or id) within one school and return their timetable.
    Returns None when the student is not found at this school. Uses cache."""
    if student_id and not name:
        sid = str(student_id)
        students = _get_students_cached(client, sub)
        match = next((s for s in students
                  if str(getattr(s, "person_id", "")) == sid), None)
        if match is None:
            return None
        name = _student_name(match)
    try:
        student = _find_student(client, name, sub)
    except RuntimeError:
        return None
    lessons = _get_student_timetable(client, sub, student, d)
    sid = int(student.person_id)
    student_name = _student_name(student) or str(sid)
    return {"student": student_name, "student_id": sid,
            "class_id": getattr(student, "class_id", None),
            "date": d.isoformat(), "subdomain": sub, "lessons": lessons,
            "_student_obj": student}


def _resolve_edustudent(client, person_id):
    """Return a concrete edupage-api EduStudent for `person_id`, or None.

    Upstream `get_timetable` keys its lookup by the *exact* Python type of the
    target (`timetables.py: lookup.get(type(target))`), so passing a skeleton or
    a SimpleNamespace from our roster cache raises `TypeError: cannot unpack
    non-iterable NoneType object`. A real EduStudent (as returned by
    `client.get_students()` for a parent's children) must be resolved before
    calling upstream."""
    pid = str(person_id)
    for s in (client.get_students() or []):
        if s is None:
            continue
        if str(getattr(s, "person_id", "")) == pid:
            return s if isinstance(s, EduStudent) else None
    for s in (client.get_all_students() or []):
        if s is None:
            continue
        if str(getattr(s, "person_id", "")) == pid and isinstance(s, EduStudent):
            return s
    return None


def _get_student_timetable(client, sub, student, d):
    """Get timetable for a student object, handling parent/teacher/student role.
    Returns list of serialized lessons."""
    sid = int(student.person_id)
    role = _roles.get(sub, "student")
    is_parent = role == "parent"

    if is_parent:
        # Preferred path: query the child's timetable directly with a concrete
        # EduStudent. The upstream switch_to_child + get_my_timetable path is
        # broken for parents (IndexError inside __get_date_plan) and, when the
        # following switch_to_parent fails, leaves the shared session stuck in
        # child mode — corrupting every later call. Avoid switching entirely.
        real = _resolve_edustudent(client, sid)
        if real is not None:
            try:
                tt = client.get_timetable(real, d)
                return [_serialize(ls) for ls in tt.lessons] if tt else []
            except Exception:
                pass  # degrade to the guarded switch path below
        # Last-resort switch path (a few schools require a child session). Never
        # hand an untyped object to upstream and always restore the session.
        try:
            client.switch_to_child(sid)
            tt = client.get_my_timetable(d)
            return [_serialize(ls) for ls in tt.lessons] if tt else []
        except Exception:
            if real is not None:
                try:
                    tt = client.get_timetable(real, d)
                    return [_serialize(ls) for ls in tt.lessons] if tt else []
                except Exception:
                    return []
            return []
        finally:
            try:
                client.switch_to_parent()
            except Exception:
                pass
    elif role == "teacher":
        target_student = student
        if isinstance(target_student, EduStudentSkeleton):
            target_student = next(
                (s for s in (client.get_students() or []) if str(getattr(s, "person_id", "")) == str(sid)),
                student,
            )
        if isinstance(target_student, EduStudentSkeleton):
            return []
        tt = client.get_timetable(target_student, d)
        return [_serialize(ls) for ls in tt.lessons] if tt else []
    else:
        tt = client.get_my_timetable(d)
        return [_serialize(ls) for ls in tt.lessons] if tt else []


@_tool
def get_student_timetable(name: str = None, student_id: str = None, date_str: str = None, subdomain: str = None) -> dict:
    """Get a student's timetable by first/last name OR person_id. Read-only.
    Without a `subdomain`, searches every school in the discovery scope (the
    configured `EDUPAGE_SUBDOMAINS`, or all logged-in schools when unset) and
    returns one result per school where the student is found — so a student
    attending multiple schools yields separate per-school timetables.

    Args:
        name: Student's first/last/full name.
        student_id: person_id (preferred — unambiguous, see `find_student`).
        date_str: YYYY-MM-DD (default today).
        subdomain: Restrict to one school (default: all logged-in schools).

    Returns:
        dict: {'results': [{student, student_id, class_id, date, subdomain,
        lessons}]}; with a `query`/`matched_schools` summary when more than one
        school is searched.

    Notes:
        - If logged in as a parent this resolves the child agent-side and
          queries their timetable directly (no session switching). For your own
          timetable use `get_my_timetable`.
        - Use the `student_id` from `find_student` / `get_my_students` for
          unambiguous lookups.
    """
    def go():
        d = _parse_date(date_str)
        if not name and not student_id:
            raise RuntimeError("Provide `name` or `student_id` for the student.")
        if subdomain:
            client = _require_client(subdomain)
            result = _student_timetable_at(client, _resolve_subdomain(subdomain), name, student_id, d)
            if result is None:
                raise RuntimeError(f"No student found at subdomain '{subdomain}'.")
            return {"results": [result]}
        schools = [s for s in _discovery_subdomains()]
        if not schools:
            raise RuntimeError("Not logged in to any school. Set EDUPAGE_SUBDOMAINS (or call `login_all`) first.")
        results = []
        for sub in schools:
            client = _clients[sub]
            if client is None or not client.is_logged_in:
                continue
            r = _student_timetable_at(client, sub, name, student_id, d)
            if r is not None:
                results.append(r)
        if not results:
            raise RuntimeError(f"No student named '{name}' found in any logged-in school {schools}.")
        return {"results": results, "query": name or student_id,
                "matched_schools": len(results)}

    return _run(go, "get_student_timetable")


def _target_timetable_day(client, target_type: str, target_id: str, d, subdomain=None):
    """Single-day timetable for a teacher/student/class/classroom as a dict."""
    target = _resolve_target(client, target_type, target_id)
    try:
        tt = client.get_timetable(target, d)
    except (IndexError, KeyError, AttributeError, TypeError):
        tt = None
    base = {"target": f"{target_type}:{target_id}", "date": d.isoformat(),
            "subdomain": _resolve_subdomain(subdomain)}
    if tt is None:
        base["lessons"] = []
    else:
        base["lessons"] = [_serialize(ls) for ls in tt.lessons]
    return base


@_tool
def get_timetable(target_type: str, target_id: str, date_str: str = None, end_date: str = None, subdomain: str = None) -> dict:
    """Get the timetable for a teacher, student, class or classroom on a date
    (or a date range, see `end_date`). Read-only.

    Args:
        target_type: 'teacher' | 'student' | 'class' | 'classroom'.
        target_id: person/class/classroom id as returned by `get_roster`.
        date_str: Single day, YYYY-MM-DD (default today). Ignored when
            `end_date` is given.
        end_date: When set, returns the timetable for every day from
            `date_str` (default today) to `end_date` inclusive, keyed by date
            — the equivalent of the former `get_timetable_range`.
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        Single-day shape: {'target', 'date', 'subdomain', 'lessons'}. Range
        shape: {'subdomain', 'range': {<YYYY-MM-DD>: single-day result}}.
        Days with no published data get an empty lessons list.

    Notes:
        - For the *logged-in user's own* timetable prefer `get_my_timetable`;
          for a student by name/id use `get_student_timetable`.
        - Any school's whole-week plan for yourself: `get_next_week_timetable`.
    """
    def go():
        client = _require_client(subdomain)
        sub = _resolve_subdomain(subdomain)
        if end_date:
            start = _parse_date(date_str)
            end = _parse_date(end_date)
            if end < start:
                raise RuntimeError("end_date must be >= date_str (or today).")
            result: dict = {}
            cur = start
            while cur <= end:
                d = cur.isoformat()
                result[d] = _target_timetable_day(client, target_type, target_id, cur, sub)
                cur += _dt.timedelta(days=1)
            return {"subdomain": sub, "range": result}
        return _target_timetable_day(client, target_type, target_id, _parse_date(date_str), sub)

    return _run(go, "get_timetable")


@_tool
def get_next_ringing_time(date_time_str: str = None, subdomain: str = None) -> dict:
    """Get the type (break/lesson) and time of the next school-bell ringing. Read-only.

    Args:
        date_time_str: ISO datetime to search onward from (default: now).
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        Serialized ringing: type (break/lesson) and time.

    Notes:
        - See `get_periods` for the full bell schedule.
    """
    def go():
        client = _require_client(subdomain)
        if date_time_str:
            dt = datetime.fromisoformat(date_time_str)
        else:
            dt = datetime.now()
        ring = client.get_next_ringing_time(dt)
        return _serialize(ring)

    return _run(go, "get_next_ringing_time")


@_tool
def get_next_week_timetable(subdomain: str = None) -> dict:
    """Get the Mon-Fri timetable for next week for the logged-in user,
    grouped by weekday. Read-only.

    Args:
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'monday', 'subdomain', 'week': [{weekday, date, lessons} x5]}.

    Notes:
        - Weekdays are 'Po','Ut','St','Št','Pi'.
        - For the logged-in user on a single day use `get_my_timetable`; for
          any target (teacher/class/room/student) over a range use
          `get_timetable` with `end_date`.
    """
    def go():
        client = _require_client(subdomain)
        today = date.today()
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        monday = today + _dt.timedelta(days=days_until_monday)
        week = []
        for i in range(5):
            d = monday + _dt.timedelta(days=i)
            try:
                tt = client.get_my_timetable(d)
            except (IndexError, KeyError, AttributeError, TypeError):
                tt = None
            if tt is None:
                lessons = []
            else:
                lessons = [_serialize(ls) for ls in tt.lessons]
            week.append({
                "weekday": ["Po", "Ut", "St", "Št", "Pi"][i],
                "date": d.isoformat(),
                "lessons": lessons,
            })
        return {"monday": monday.isoformat(), "subdomain": _resolve_subdomain(subdomain), "week": week}

    return _run(go, "get_next_week_timetable")


@_tool
def get_periods(subdomain: str = None) -> dict:
    """Get the bell schedule (periods with start/end times). Read-only.

    Args:
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'periods': [{'starttime', 'endtime'}, ...]}.

    Notes:
        - Combine with `get_next_ringing_time` for live bell timing.
    """
    def go():
        client = _require_client(subdomain)
        if client.data is None:
            raise RuntimeError("No login data available.")
        zv = client.data.get("zvonenia") or []
        return {"periods": [{"starttime": p.get("starttime"), "endtime": p.get("endtime")} for p in zv]}

    return _run(go, "get_periods")


# --------------------------------------------------------------------------
# Grades
# --------------------------------------------------------------------------
@_tool
def get_grades(year: int = None, term: str = None, subdomain: str = None) -> dict:
    """Get grades for the logged-in student. Read-only.

    Args:
        year: School-year start year to filter by (e.g. 2025 for 2025/26).
        term: 'FIRST' or 'SECOND' to restrict the term.
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'subdomain', 'grades': [serialized grades with subject, teacher,
        percent, ...]}.

    Notes:
        - When both `year` and `term` are omitted returns the current gradebook.
        - Use `get_school_year` to resolve the current school-year start.
    """
    def go():
        client = _require_client(subdomain)
        if year or term:
            t = Term.FIRST if term == "FIRST" else Term.SECOND if term == "SECOND" else None
            if t is None:
                raise RuntimeError("term must be 'FIRST' or 'SECOND'.")
            grades = client.get_grades_for_term(int(year), t)
        else:
            grades = client.get_grades()
        return {"subdomain": _resolve_subdomain(subdomain), "grades": [_serialize(g) for g in grades]}

    return _run(go, "get_grades")


# --------------------------------------------------------------------------
# Notifications / timeline (homework, exams, messages...)
# --------------------------------------------------------------------------
@_tool
def get_timeline(category: str = "recent", date_from: str = None, subdomain: str = None) -> dict:
    """Get EduPage timeline notifications, filtered by category. Read-only.

    Args:
        category: Which event types to return:
            - 'recent' (default) — all currently visible notifications
              (homework, tests, messages, grades, events...).
            - 'history' — all notifications since `date_from` (incl. older ones).
            - 'homework' — homework assignments (formerly `get_homework`).
            - 'assignments' — homework, tests, exams and projects
              (formerly `get_assignments`).
            - 'absences' — absence records (formerly `get_absences`).
            - 'events' — upcoming events: trips, excursions, meetings, holidays...
              (formerly `get_upcoming_events`).
            - 'news' — school news (formerly `get_news`).
        date_from: YYYY-MM-DD. Only meaningful for category='history'.
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict with subdomain and the matching notifications, e.g.
        {'subdomain': ..., 'notifications': [...]} (key is the category name).

    Notes:
        - Categories are derived from timeline notifications; a school that
          doesn't publish a given event type returns an empty list.
        - For a whole-day report (timetable, substitutions, meals, homework,
          events, news, grades) prefer `get_day_summary` — one call.
    """
    def go():
        client = _require_client(subdomain)
        sub = _resolve_subdomain(subdomain)
        if category == "history":
            if not date_from:
                raise RuntimeError("category='history' requires `date_from` (YYYY-MM-DD).")
            d = _parse_date(date_from)
            events = client.get_notification_history(d)
            return {"subdomain": sub, "category": category,
                    "notifications": [_serialize(e) for e in events]}
        if category == "recent":
            events = client.get_notifications()
            return {"subdomain": sub, "category": category,
                    "notifications": [_serialize(e) for e in events]}
        type_set = {
            "homework": _HOMEWORK_TYPES,
            "assignments": _EXAM_TYPES,
            "absences": _ABSENCE_TYPES,
            "events": _EVENT_TYPES,
            "news": {EventType.NEWS},
        }.get(category)
        if type_set is None:
            raise RuntimeError(
                f"category must be one of: recent, history, homework, "
                f"assignments, absences, events, news."
            )
        events = client.get_notifications()
        result = [_serialize(e) for e in events if e.event_type in type_set]
        return {"subdomain": sub, "category": category, category: result}

    return _run(go, "get_timeline")


# --------------------------------------------------------------------------
# Substitutions / teachers
# --------------------------------------------------------------------------
def _get_changes_for(client, sub, d):
    """Timetable changes (substitutions) for a date, with re-login handling."""
    try:
        changes = client.get_timetable_changes(d)
    except edupage_exceptions.ExpiredSessionException:
        if not _relogin_subdomain(sub):
            changes = []
        else:
            client = _require_client(sub)
            try:
                changes = client.get_timetable_changes(d)
            except edupage_exceptions.ExpiredSessionException:
                changes = []
    return [_serialize(c) for c in changes] if changes is not None else []


def _get_missing_teachers_for(client, sub, d):
    """Missing teachers for a date, with re-login handling."""
    try:
        teachers = client.get_missing_teachers(d)
    except edupage_exceptions.ExpiredSessionException:
        if not _relogin_subdomain(sub):
            teachers = []
        else:
            client = _require_client(sub)
            try:
                teachers = client.get_missing_teachers(d)
            except edupage_exceptions.ExpiredSessionException:
                teachers = []
    return [_serialize(t) for t in teachers or []]


@_tool
def get_timetable_changes(date_str: str = None, subdomain: str = None) -> dict:
    """Get substitution/timetable changes for a date (default today). Read-only.

    Args:
        date_str: YYYY-MM-DD (default today).
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'date', 'subdomain', 'changes': [serialized substitutions]}.
        Empty list when nothing changed or the school publishes none.

    Notes:
        - Pair with `get_missing_teachers` for the full substitution picture.
        - For one student's plan on a day use `get_student_timetable` / `get_timetable`.
    """
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        sub = _resolve_subdomain(subdomain)
        return {"date": d.isoformat(), "subdomain": sub, "changes": _get_changes_for(client, sub, d)}

    return _run(go, "get_timetable_changes")


@_tool
def get_missing_teachers(date_str: str = None, subdomain: str = None) -> dict:
    """Get teachers missing on a date (default today). Read-only.

    Args:
        date_str: YYYY-MM-DD (default today).
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'date', 'subdomain', 'teachers': [serialized missing teachers]}.
        Empty list when no teacher is missing.

    Notes:
        - Pair with `get_timetable_changes` for the full substitution picture.
    """
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        sub = _resolve_subdomain(subdomain)
        return {"date": d.isoformat(), "subdomain": sub,
                "teachers": _get_missing_teachers_for(client, sub, d)}

    return _run(go, "get_missing_teachers")


# --------------------------------------------------------------------------
# Meals
# --------------------------------------------------------------------------
# edupage-api 0.12.5's `get_meals` parses the per-student meal-ordering page
# ("novyListok") only in the shape where each day is a dict keyed "1"/"2"/"3",
# and crashes (AttributeError) when a school publishes the day as a list of
# serving windows instead. We therefore keep two wrapper-level fallbacks so
# `get_meals` still returns the menu:
#   1. the school's public "Canteen Menu" widget (schools that don't enable the
#      per-student app), and
#   2. the "novyListok" page itself when its day entry is a list of serving
#      windows (each carrying vydaj_od/vydaj_do and per-menu rows) that the
#      personal-ordering UI renders. Position (list) or key (sparse dict on
#      weekends) in the day entry is the meal slot, matching the map below.
_CANTEEN_WIDGET = "menu_CanteenMenu_1"
# meal slot index -> slot. 0/4 (breakfast/dinner) are extras beyond the standard
# snack/lunch/afternoon_snack contract that edupage-api uses.
_MEAL_SLOTS = {0: "breakfast", 1: "snack", 2: "lunch", 3: "afternoon_snack", 4: "dinner"}


def _strip_html_text(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", value))).strip()


def _split_food_weight(food):
    m = re.search(r"\((\d+)\)\s*$", food)
    if m:
        return food[: m.start()].strip(), m.group(1)
    return food, ""


def _parse_canteen_menu(text, day):
    """Parse the school's public canteen widget HTML for one date.

    Returns a dict keyed by meal slot -> plain meal dict (or None), e.g.
    {"breakfast": None, "snack": {...}, "lunch": {...}, "afternoon_snack": ...,
     "dinner": ...}. Returns None when the widget isn't present in the page.
    """
    if f'id="{_CANTEEN_WIDGET}"' not in text:
        return None

    target = day.isoformat()

    def lines(block, key):
        start = block.find(key)
        if start == -1:
            return []
        open_pos = block.find(">", start) + 1
        close = block.find("</div>", open_pos)
        segment = block[open_pos:close] if close != -1 else block[open_pos:]
        parts = [_strip_html_text(p) for p in re.split(r"<br\s*/?>", segment)]
        return [p for p in parts if p and "skgd" not in p and "Div_" not in p]

    by_day = {}
    for li in re.split(r"(?i)(?=<\s*li\b)", text):
        m = re.search(r'data-listItemId="([^"]+)"', li)
        if not m:
            continue
        item_id = m.group(1)
        if item_id.startswith("__mt"):
            continue
        dm = re.match(r"(\d{4}-\d{2}-\d{2})-(\d+)$", item_id)
        if not dm or dm.group(1) != target:
            continue
        idx = int(dm.group(2))
        if idx not in _MEAL_SLOTS:
            continue
        title_m = re.search(r"menu_DFText_3\"[^>]*>\s*([^<]+?)\s*</span>", li)
        by_day.setdefault(dm.group(1), {})[idx] = {
            "title": title_m.group(1).strip() if title_m else _MEAL_SLOTS[idx],
            "foods": lines(li, "menu_DFText_4"),
            "allergens": lines(li, "menu_DFText_5"),
        }

    result = {slot: None for slot in _MEAL_SLOTS.values()}
    for idx, slot in _MEAL_SLOTS.items():
        item = by_day.get(target, {}).get(idx)
        if not item:
            continue
        menus = []
        foods = item["foods"]
        for i, food in enumerate(foods):
            name, weight = _split_food_weight(food)
            allergens = item["allergens"][i] if i < len(item["allergens"]) else ""
            menus.append({"name": name, "allergens": allergens, "weight": weight,
                          "number": None, "rating": None})
        result[slot] = {
            "served_from": None,
            "served_to": None,
            "amount_of_foods": len(menus),
            "chooseable_menus": [],
            "can_be_changed_until": None,
            "title": item["title"],
            "menus": menus,
            "date": target,
            "ordered_meal": None,
            "meal_type": idx,
        }
    return result


def _fetch_canteen_menu(client, day):
    sub = client.subdomain
    url = f"https://{sub}.edupage.org/menu/?wid={_CANTEEN_WIDGET}&date={day.isoformat()}"
    resp = client.session.get(url)
    return _parse_canteen_menu(resp.content.decode("utf-8", "replace"), day)


def _strip_recipe_code(name):
    """Drop recipe-code prefixes like '2.065 ' from meal names, per line."""
    return "\n".join(re.sub(r"^\s*\d+\.\d+\s+", "", line)
                     for line in str(name).splitlines())


def _fetch_novylistok(client, day):
    """Fetch the per-student meal-ordering page and parse day entries published
    in the serving-windows (list) shape that edupage-api 0.12.5 cannot handle.
    Returns the same slot-keyed dict as _parse_canteen_menu, or None when the
    page doesn't carry a day entry (school uses the old shape / no meals)."""
    sub = client.subdomain
    url = f"https://{sub}.edupage.org/menu/?date={day.strftime('%Y%m%d')}"
    resp = client.session.get(url)
    text = resp.content.decode("utf-8", "replace")
    try:
        payload = json.loads(text.split("edupageData: ")[1].split(",\r\n")[0])
    except (IndexError, ValueError):
        return None
    nl = (payload or {}).get(sub, {}).get("novyListok", {})
    entry = nl.get(day.isoformat())
    if not entry:
        return None
    items = entry.items() if isinstance(entry, dict) else enumerate(entry)
    result = {slot: None for slot in _MEAL_SLOTS.values()}
    for pos, item in items:
        try:
            idx = int(pos)
        except (TypeError, ValueError):
            continue
        slot = _MEAL_SLOTS.get(idx)
        if not slot or not isinstance(item, dict):
            continue
        menu_defs = item.get("menus") or {}
        if not isinstance(menu_defs, dict) or not menu_defs:
            continue
        default_key = sorted(menu_defs, key=str)[0]
        default_menu = menu_defs.get(default_key) or {}

        def row_dict(row):
            row = row or {}
            return {"name": _strip_recipe_code(row.get("nazov") or ""),
                    "allergens": row.get("alergenyStr") or "",
                    "weight": row.get("hmotnostiStr") or "",
                    "number": None, "rating": None}

        menus = [row_dict(r) for r in (default_menu.get("rows") or [])]
        chooseable_menus = [
            {"title": md.get("nazovMenu"),
             "foods": [_strip_recipe_code(r.get("nazov") or "")
                       for r in (md.get("rows") or [])]}
            for key, md in menu_defs.items()
            if key != default_key and isinstance(md, dict)
        ]
        result[slot] = {
            "served_from": item.get("vydaj_od"),
            "served_to": item.get("vydaj_do"),
            "amount_of_foods": len(menus),
            "chooseable_menus": chooseable_menus,
            "can_be_changed_until": item.get("prihlas_do"),
            "title": _strip_recipe_code(item.get("nazov") or ""),
            "menus": menus,
            "date": day.isoformat(),
            "ordered_meal": None,
            "meal_type": idx,
        }
    return result


_ALL_MEAL_SLOTS = ("breakfast", "snack", "lunch", "afternoon_snack", "dinner")


def _meals_payload(client, d, sub):
    """Meal menu payload for a date: personal ordering endpoint first, then the
    school's public canteen widget. Always returns all five meal slots
    (breakfast/snack/lunch/afternoon_snack/dinner) — slots not published by
    the school are ``None``.  Pure function usable by both get_meals and
    get_day_summary."""
    _none = {k: None for k in _ALL_MEAL_SLOTS}

    meals = None
    try:
        meals = client.get_meals(d)
    except (edupage_exceptions.InvalidMealsData, IndexError, AttributeError, KeyError, TypeError):
        meals = None
    except edupage_exceptions.ExpiredSessionException:
        if _relogin_subdomain(sub):
            client = _require_client(sub)
            meals = client.get_meals(d)
        else:
            raise

    # Path 1: personal endpoint has at least one of snack/lunch/afternoon_snack.
    # Supplement missing breakfast/dinner from the public canteen widget.
    if meals is not None and any(getattr(meals, m) for m in ("snack", "lunch", "afternoon_snack")):
        meals_data = _serialize(meals)
        for slot in _ALL_MEAL_SLOTS:
            meals_data.setdefault(slot, None)
        if not meals_data["breakfast"] or not meals_data["dinner"]:
            for fetch in (_fetch_canteen_menu, _fetch_novylistok):
                try:
                    extra = fetch(client, d)
                except Exception:
                    extra = None
                if extra is None:
                    continue
                if not meals_data["breakfast"]:
                    meals_data["breakfast"] = extra.get("breakfast")
                if not meals_data["dinner"]:
                    meals_data["dinner"] = extra.get("dinner")
                break
        return meals_data

    # Path 2: personal endpoint unavailable — try the public widget for all slots.
    for fetch in (_fetch_canteen_menu, _fetch_novylistok):
        try:
            extra = fetch(client, d)
        except Exception:
            extra = None
        if extra is None:
            continue
        return {k: extra.get(k) for k in _ALL_MEAL_SLOTS}

    return _none


@_tool
def get_meals(date_str: str = None, subdomain: str = None) -> dict:
    """Get the meal menu for a date. Read-only. Always returns all five meal
    slots (breakfast, snack, lunch, afternoon_snack, dinner) — slots not
    published by the school are ``None``.

    Args:
        date_str: YYYY-MM-DD (default today).
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'date', 'subdomain', 'meals': {breakfast/snack/lunch/
        afternoon_snack/dinner: menus}}. Each menu carries chooseable/ordered
        info usable with `choose_meal` / `sign_off_meal`.

    Notes:
        - Tries the personal ordering endpoint first; when the school hasn't
          enabled it, falls back to the school's public canteen menu widget.
    """
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        sub = _resolve_subdomain(subdomain)
        return {"date": d.isoformat(), "subdomain": sub,
                "meals": _meals_payload(client, d, sub)}

    return _run(go, "get_meals")


@_tool
def choose_meal(date_str: str, meal_type: str, number: int, subdomain: str = None) -> dict:
    """Order/choose a meal. Writes: books the selected menu for the date.

    Args:
        date_str: YYYY-MM-DD to order for.
        meal_type: 'snack' | 'lunch' | 'afternoon_snack'.
        number: 1-based menu choice among the chooseable menus (see `get_meals`).
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'ordered': True, meal_type, date, number}.

    Notes:
        - Read `get_meals` first for the date to pick a valid `number`.
        - To cancel, use `sign_off_meal`.
    """
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        meals = client.get_meals(d)
        if meals is None:
            raise RuntimeError(f"No meals available for {d.isoformat()}.")
        meal = getattr(meals, meal_type, None)
        if meal is None:
            raise RuntimeError(f"No '{meal_type}' meal available for {d.isoformat()}.")
        meal.choose(client, number)
        return {"ordered": True, "meal_type": meal_type, "date": d.isoformat(), "number": number}

    return _run(go, "choose_meal")


@_tool
def sign_off_meal(date_str: str, meal_type: str, subdomain: str = None) -> dict:
    """Cancel an ordered meal for a date. Writes: releases the booking.

    Args:
        date_str: YYYY-MM-DD to cancel.
        meal_type: 'snack' | 'lunch' | 'afternoon_snack'.
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'ordered': False, meal_type, date}.
    """
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        meals = client.get_meals(d)
        meal = getattr(meals, meal_type, None) if meals else None
        if meal is None:
            raise RuntimeError(f"No '{meal_type}' meal available for {d.isoformat()}.")
        meal.sign_off(client)
        return {"ordered": False, "meal_type": meal_type, "date": d.isoformat()}

    return _run(go, "sign_off_meal")


@_tool
def rate_meal(date_str: str, meal_type: str, quality: int, quantity: int, subdomain: str = None) -> dict:
    """Rate a meal. Writes: submits quality/quantity ratings for a date and meal type.

    Args:
        date_str: YYYY-MM-DD of the meal.
        meal_type: 'snack' | 'lunch' | 'afternoon_snack'.
        quality: Taste rating, 1-5.
        quantity: Portion-size rating, 1-5.
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'rated': True, meal_type, date}.
    """
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        meals = client.get_meals(d)
        meal = getattr(meals, meal_type, None) if meals else None
        if meal is None:
            raise RuntimeError(f"No '{meal_type}' meal available for {d.isoformat()}.")
        rating_boarder = None
        for menu in meal.menus or []:
            if menu.rating is not None:
                rating_boarder = menu.rating
                break
        if rating_boarder is None:
            raise RuntimeError("No rating available for this meal.")
        rating_boarder.rate(client, quantity, quality)
        return {"rated": True, "meal_type": meal_type, "date": d.isoformat()}

    return _run(go, "rate_meal")


# --------------------------------------------------------------------------
# Day summary (composite report)
# --------------------------------------------------------------------------
# get_day_summary assembles the per-section tools into one call so an agent can
# answer "what happened yesterday at school / what's coming tomorrow" without
# firing 8-10 separate tools. Each section is isolated: a failure in one
# section (e.g. no gradebook, no timeline access) yields {"ok": false, ...}
# without failing the whole report.
_HOMEWORK_TYPES = {EventType.HOMEWORK, EventType.HOMEWORK_STUDENT_STATE}
_EXAM_TYPES = {
    EventType.BIG_EXAM, EventType.HOMEWORK, EventType.ORAL_EXAM,
    EventType.PAPER, EventType.PROJECT_EXAM, EventType.SHORT_EXAM,
    EventType.TESTING, EventType.HOMEWORK_STUDENT_STATE,
    EventType.EXAM_ASSIGNMENT, EventType.EXAM_EVALUATION,
    EventType.TEST_RESULT,
}
_ABSENCE_TYPES = {EventType.STUDENT_ABSENT, EventType.EXCUSED_LESSON, EventType.REPRESENTATION}
_EVENT_TYPES = {
    EventType.EVENT, EventType.SCHOOL_EVENT, EventType.EXCURSION,
    EventType.SCHOOL_TRIP, EventType.PARENTS_EVENING, EventType.TEACHER_MEETING,
    EventType.CULTURE, EventType.FREE_DAY, EventType.HOLIDAY, EventType.SHORT_HOLIDAY,
    EventType.SCHOOL_TEACHER_EVENT if hasattr(EventType, "SCHOOL_TEACHER_EVENT") else None,
}
_EVENT_TYPES.discard(None)


def _timeline_on_day(client, d, types):
    """Timeline notifications whose timestamp falls on date `d`, optionally
    restricted to a set of event types (None = all types)."""
    events = client.get_notifications() or []
    result = []
    for e in events:
        ts = getattr(e, "timestamp", None)
        if ts is None or ts.date() != d:
            continue
        if types is not None and e.event_type not in types:
            continue
        result.append(_serialize(e))
    return result


def _my_timetable_lessons(client, d):
    try:
        tt = client.get_my_timetable(d)
    except (IndexError, KeyError, AttributeError, TypeError):
        tt = None
    return [_serialize(ls) for ls in tt.lessons] if tt else []


def _grades_on_day(client, d):
    grades = client.get_grades() or []
    received = [g for g in grades if (getattr(g, "date", None) or datetime.min).date() == d]
    return {"received": [_serialize(g) for g in received], "total_in_period": len(grades)}


@_tool
def get_day_summary(date_str: str = None, name: str = None, student_id: str = None,
                    subdomain: str = None, full: bool = False) -> dict:
    """One-call daily school report for a date (default today): timetable,
    substitutions, missing teachers, grades received that day, meals, homework,
    assignments, absences, news, events, and timeline notifications.

    Composes the individual section tools so you don't need to fire 8-10 calls
    to answer "what happened yesterday at school" or "what's coming tomorrow".
    - If `name`/`student_id` is provided: report for that specific student
      (found across all schools unless `subdomain` scopes it).
    - If omitted: **discovery-first** — for a **parent** this returns a
      lightweight per-school index of the account's children (no per-child
      section fetching), so you can then call per child with `name`/`student_id`.
      Set `full=True` to instead build the full report for every child.
    - If omitted and logged in as student/teacher: report on the logged-in account.
    Every section is isolated — a failure in one section yields {"ok": false, "error": ...}
    without failing the report."""
    def go():
        d = _parse_date(date_str)
        discovery_errors = []
        if subdomain:
            subs = [_resolve_subdomain(subdomain)]
        else:
            subs = []
            for sub in _discovery_subdomains():
                block = _login_block_message(sub)
                if block:
                    discovery_errors.append((sub, block))
                else:
                    subs.append(sub)
        student_query = name or student_id
        schools_out = []

        # Determine target students per school
        targets_per_school = {}
        if student_query:
            # Explicit student requested
            for sub in subs:
                client = _require_client(sub)
                try:
                    r = _student_timetable_at(client, sub, name, student_id, d)
                except Exception as e:  # noqa: BLE001
                    discovery_errors.append((sub, f"Could not resolve '{student_query}' at '{sub}': {type(e).__name__}: {e}"))
                    continue
                if r is not None:
                    targets_per_school.setdefault(sub, []).append({
                        "name": r["student"], "student_id": r["student_id"],
                        "class_id": r["class_id"], "student_obj": r.get("_student_obj"),
                        "lessons": r["lessons"]
                    })
        else:
            # No explicit student: auto-discover based on role
            for sub in subs:
                client = _require_client(sub)
                role = _roles.get(sub, "student")
                if role == "parent":
                    # Parent: get all children
                    try:
                        students = _get_students_cached(client, sub)
                    except Exception as e:  # noqa: BLE001
                        discovery_errors.append((sub, f"Could not discover children at '{sub}': {type(e).__name__}: {e}"))
                        continue
                    for student in students:
                        targets_per_school.setdefault(sub, []).append({
                            "name": _resolve_student_full_name(client, sub, student),
                            "student_id": getattr(student, "person_id", None),
                            "class_id": getattr(student, "class_id", None),
                            "student_obj": student
                        })
                else:
                    # Student/teacher: self
                    targets_per_school.setdefault(sub, []).append({
                        "name": "self", "student_id": None,
                        "class_id": None, "student_obj": None
                    })

        # Add error entries for schools that failed discovery
        for sub, err in discovery_errors:
            schools_out.append({"subdomain": sub, "date": d.isoformat(), "error": err})

        # Discovery-first: when no student is named and the caller did not ask
        # for full reports, return a lightweight per-school index of the account's
        # children instead of building (possibly slow, and easily conflated)
        # per-child full reports across every school in one payload.
        if not student_query and not full and any(
                t.get("student_id") is not None
                for ts in targets_per_school.values() for t in ts):
            for sub, targets in targets_per_school.items():
                schools_out.append({
                    "subdomain": sub,
                    "date": d.isoformat(),
                    "students": [{
                        "name": t["name"],
                        "student_id": t["student_id"],
                        "class_id": t["class_id"],
                    } for t in targets],
                })
            return {"date": d.isoformat(), "mode": "discovery",
                    "message": "Discovery index — each school lists the students "
                               "visible to the logged-in account. Call `get_day_summary` "
                               "with `name` (or `student_id`) and optionally `subdomain`, "
                               "per child, to fetch a full daily report. Use `full=True` "
                               "to build full reports for all children in one call.",
                    "results": schools_out}

        # Build report for each target student
        for sub, targets in targets_per_school.items():
            try:
                client = _require_client(sub)
            except RuntimeError as e:
                schools_out.append({"subdomain": sub, "date": d.isoformat(), "error": str(e)})
                continue
            for target in targets:
                result = {"subdomain": sub, "date": d.isoformat(), "sections": {}}

                def run_section(key, fn):
                    try:
                        result["sections"][key] = {"ok": True, **fn()}
                    except Exception as e:  # noqa: BLE001
                        result["sections"][key] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

                # Timetable
                if target["student_id"] is not None:
                    # Reuse the timetable already fetched during discovery;
                    # re-resolve only for cached roster entries (auto-discovery).
                    lessons = target.get("lessons")
                    if lessons is None:
                        try:
                            if target.get("student_obj") is not None:
                                lessons = _get_student_timetable(client, sub, target["student_obj"], d)
                            else:
                                r = _student_timetable_at(client, sub, name, str(target["student_id"]), d)
                                lessons = r["lessons"] if r else []
                        except Exception:
                            lessons = []
                    result["student"] = {"name": target["name"], "student_id": target["student_id"],
                                         "class_id": target["class_id"]}
                    run_section("timetable", lambda lessons=lessons: {"lessons": lessons})
                else:
                    # Self (student/teacher account)
                    run_section("timetable", lambda: {"lessons": _my_timetable_lessons(client, d)})

                # Other sections (same for all - school-level data)
                run_section("substitutions", lambda: {"changes": _get_changes_for(client, sub, d)})
                run_section("missing_teachers", lambda: {
                    "teachers": _get_missing_teachers_for(client, sub, d)})
                run_section("grades", lambda: _grades_on_day(client, d))
                run_section("meals", lambda: {"meals": _meals_payload(client, d, sub)})
                run_section("homework", lambda: {"homework": _timeline_on_day(client, d, _HOMEWORK_TYPES)})
                run_section("assignments", lambda: {"assignments": _timeline_on_day(client, d, _EXAM_TYPES)})
                run_section("absences", lambda: {"absences": _timeline_on_day(client, d, _ABSENCE_TYPES)})
                run_section("news", lambda: {"news": _timeline_on_day(client, d, {EventType.NEWS})})
                run_section("events", lambda: {"events": _timeline_on_day(client, d, _EVENT_TYPES)})
                run_section("notifications", lambda: {"notifications": _timeline_on_day(client, d, None)})
                schools_out.append(result)

        if not schools_out:
            if discovery_errors:
                raise RuntimeError(f"Could not build a report: {discovery_errors[0][1]}")
            raise RuntimeError(f"No data found for {student_query or 'logged-in account'} on {d.isoformat()}.")
        return {"date": d.isoformat(), "student_query": student_query or None,
                "results": schools_out}

    return _run(go, "get_day_summary")


# --------------------------------------------------------------------------
# Rosters
# --------------------------------------------------------------------------
@_tool
def get_roster(roster_type: str, subdomain: str = None) -> dict:
    """Get a school roster: students, teachers, classes, classrooms or subjects.
    Read-only.

    Args:
        roster_type: Which roster to return:
            - 'students' — students in the logged-in user's class
              (formerly `get_students`).
            - 'all_students' — a short list of all students in the school
              (formerly `get_all_students`).
            - 'teachers' — all teachers (formerly `get_teachers`).
            - 'classes' — all classes (formerly `get_classes`).
            - 'classrooms' — all classrooms (formerly `get_classrooms`).
            - 'subjects' — all subjects (formerly `get_subjects`).
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict keyed by the roster name, e.g.
        {'subdomain': ..., 'teachers': [...], ...}.

    Notes:
        - For the students *visible to the logged-in account* (parents: their
          linked children; students: classmates) prefer `get_my_students`.
        - To look one student up by name use `find_student`.
        - Returned person/class ids feed `get_timetable` (target_type/`target_id`)
          and `switch_to_student`.
    """
    def go():
        client = _require_client(subdomain)
        sub = _resolve_subdomain(subdomain)
        # Direct call per branch so the upstream-coverage AST checker sees every
        # client.get_* method (string getattr indirection would hide them).
        if roster_type == "students":
            items = client.get_students() or []
            key = "students"
        elif roster_type == "all_students":
            items = client.get_all_students() or []
            key = "students"
        elif roster_type == "teachers":
            items = client.get_teachers() or []
            key = "teachers"
        elif roster_type == "classes":
            items = client.get_classes() or []
            key = "classes"
        elif roster_type == "classrooms":
            items = client.get_classrooms() or []
            key = "classrooms"
        elif roster_type == "subjects":
            items = client.get_subjects() or []
            key = "subjects"
        else:
            raise RuntimeError(
                "roster_type must be one of: all_students, students, teachers, "
                "classes, classrooms, subjects."
            )
        return {"subdomain": sub, key: [_serialize(s) for s in items]}

    return _run(go, "get_roster")


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------
@_tool
def send_message(recipient_id: str, body: str, subdomain: str = None) -> dict:
    """Send a message to a recipient. Writes: posts a new message on the
    recipient's timeline.

    Args:
        recipient_id: EduPage id like 'Student123' or 'Teacher456' (see
            `get_roster`).
        body: Message text. Must not be empty.
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'sent': True, 'timeline_id': <id>}.

    Notes:
        - Recipient ids come from `get_roster(roster_type='students'|'teachers')`
          or `get_my_students`.
    """
    def go():
        client = _require_client(subdomain)
        if not body or not body.strip():
            raise RuntimeError("body must not be empty.")
        timeline_id = client.send_message(recipient_id, body)
        return {"sent": True, "timeline_id": timeline_id}

    return _run(go, "send_message")


# --------------------------------------------------------------------------
# Students / accounts
# --------------------------------------------------------------------------
@_tool
def get_my_students(subdomain: str = None) -> dict:
    """Get the students visible to the logged-in account. Read-only: parent
    accounts see their linked children (parsed from the school homepage);
    student/teacher accounts see classmates. Uses cached data.

    Args:
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'subdomain', 'students': [{person_id, name, class_id, ...}]}
        usable with `switch_to_student` and `get_student_timetable`.

    Notes:
        - Prefer `get_my_students` over `get_roster` to see *your* children /
          classmates; `get_roster(roster_type='all_students')` lists the whole
          school.
        - Cache is refreshed by `clear_student_cache`; `scan_students` returns
          the same visibility across all schools.
    """
    def go():
        client = _require_client(subdomain)
        sub = _resolve_subdomain(subdomain)
        role = _roles.get(sub, "student")
        students = _get_students_cached(client, sub)
        if role == "parent":
            serialized = []
            for s in students:
                d = _serialize(s)
                d["name"] = _resolve_student_full_name(client, sub, s)
                serialized.append(d)
            return {"subdomain": sub, "students": serialized}
        classmates = []
        for s in students:
            classmates.append({
                "person_id": s.person_id,
                "name": _resolve_student_full_name(client, sub, s),
                "class_id": getattr(s, "class_id", None),
                "number": getattr(s, "number_in_class", None),
            })
        return {"subdomain": sub, "students": classmates}

    return _run(go, "get_my_students")


@_tool
def switch_to_student(student_id: str = None, name: str = None, subdomain: str = None) -> dict:
    """Switch the session to a student account (parent accounts only). Writes:
    changes which account subsequent tools operate as.

    Args:
        student_id: person_id of the child (from `get_my_students`).
        name: first/last/full name of the child, used when `student_id` is omitted.
        subdomain: School to query (defaults to the active subdomain).

    Returns:
        dict: {'switched_to_student': <person_id>, 'user_id': ...}.

    Notes:
        - Revert with `switch_to_parent`. Prefer the stateless
          `get_student_timetable` (name/student_id) over switching when you
          only need a timetable.
    """
    def go():
        client = _require_client(subdomain)
        sub = _resolve_subdomain(subdomain)
        if not student_id and not name:
            raise RuntimeError("Provide `student_id` or `name`.")
        cid = int(student_id) if student_id else int(_find_student(client, name, sub).person_id)
        client.switch_to_child(cid)
        return {"switched_to_student": cid, "user_id": client.get_user_id()}

    return _run(go, "switch_to_student")


@_tool
def find_student(name: str, subdomain: str = None) -> dict:
    """Look up a student by first/last/full name using tiered matching. Read-only.
    Without a `subdomain`, searches ALL logged-in schools and returns one result
    per school where the student is found.

    Args:
        name: First, last or full student name (also 'Novák V.' short names).
        subdomain: Restrict the search to one school (default: all logged-in
            schools in scope).

    Returns:
        dict with `results`: [{name, student_id, class_id, subdomain, tier,
        confidence}] sorted by confidence. Tiers: 1=exact, 2=first name,
        3=last name, 4=substring.

    Notes:
        - Use a `student_id` from the results with `get_student_timetable` /
          `get_day_summary` for unambiguous lookups.
        - Ambiguous matches surface all candidates instead of guessing.
    """
    def go():
        if not name:
            raise RuntimeError("Provide `name` for the student to find.")
        if subdomain:
            client = _require_client(subdomain)
            matches = _find_student_all(client, name, _resolve_subdomain(subdomain))
            if not matches:
                raise RuntimeError(f"No student named '{name}' found at '{subdomain}'.")
            return {"results": [{"name": m["name"], "student_id": m["student_id"],
                    "class_id": m["class_id"], "subdomain": m["subdomain"],
                    "tier": m["tier"], "confidence": m["confidence"]}
                    for m in matches],
                    "query": name, "subdomain": _resolve_subdomain(subdomain)}
        schools = [s for s in _discovery_subdomains()]
        if not schools:
            raise RuntimeError("Not logged in to any school. Set EDUPAGE_SUBDOMAINS (or call `login_all`) first.")
        all_results = []
        errors = []
        for sub in schools:
            block = _login_block_message(sub)
            if block:
                errors.append(block)
                continue
            client = _clients[sub]
            try:
                matches = _find_student_all(client, name, sub)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{sub}: {type(e).__name__}: {e}")
                continue
            for m in matches:
                all_results.append({
                    "name": m["name"], "student_id": m["student_id"],
                    "class_id": m["class_id"], "subdomain": m["subdomain"],
                    "tier": m["tier"], "confidence": m["confidence"],
                })
        if not all_results:
            if errors:
                raise RuntimeError("; ".join(errors))
            raise RuntimeError(f"No student named '{name}' found in any logged-in school {schools}.")
        # Sort by tier (best first) across all schools
        all_results.sort(key=lambda r: (r["tier"], -r["confidence"]))
        return {"results": all_results, "query": name,
                "matched_schools": len(set(r["subdomain"] for r in all_results)),
                "total_matches": len(all_results)}

    return _run(go, "find_student")


@_tool
def get_schools() -> dict:
    """List all schools the server is logged into (from auto-discovery or
    `login`/`login_all`), plus overall session status. Read-only.

    Returns:
        dict with:
        - `schools`: per subdomain {subdomain, logged_in, role
          (student/parent/teacher), user_id, two_factor_pending, active}.
        - `active_subdomain`: the school used by tools without an explicit
          `subdomain` argument.
        - `failed_logins`: subdomain → error for login attempts that failed or
          are blocked (e.g. pending 2FA at startup).
        - `env_*_set`: whether EDUPAGE_USERNAME / EDUPAGE_PASSWORD /
          EDUPAGE_SUBDOMAINS are configured.

    Notes:
        - Use this instead of the former `auth_status` / `user_id` tools.
        - A school with `two_factor_pending: True` needs `two_factor_finish`.
    """
    def go():
        schools = []
        for sub in _clients:
            client = _clients[sub]
            tf_pending = _two_factor.get(sub) is not None
            schools.append({
                "subdomain": sub,
                "logged_in": bool(client and client.is_logged_in),
                "role": _roles.get(sub),
                "user_id": client.get_user_id() if (client and client.is_logged_in) else None,
                "two_factor_pending": tf_pending,
                "active": sub == _active_subdomain,
            })
        failed = dict(_autologin_failures) if _autologin_failures else {}
        return {"schools": schools, "active_subdomain": _active_subdomain,
                "failed_logins": failed,
                "env_username_set": bool(EDUPAGE_USERNAME),
                "env_password_set": bool(EDUPAGE_PASSWORD),
                "env_subdomains_set": bool(EDUPAGE_SUBDOMAINS)}

    return _run(go, "get_schools")


@_tool
def clear_student_cache(subdomain: str = None) -> dict:
    """Force refresh of cached student data. Writes: drops the local cache so the
    next student lookup re-fetches from EduPage. Call this after students are
    added/removed from a school, or if `scan_students`/`find_student` seems stale.

    Args:
        subdomain: School whose cache to clear. Without it, clears ALL schools.

    Returns:
        dict: {'cleared': <subdomain|'all'>, 'entries_removed': <int>}.
    """
    def go():
        global _student_cache
        if subdomain:
            cleared = []
            for key in list(_student_cache.keys()):
                if key[0] == subdomain:
                    del _student_cache[key]
                    cleared.append(key)
            return {"cleared": [subdomain], "entries_removed": len(cleared)}
        count = len(_student_cache)
        _student_cache = {}
        return {"cleared": "all", "entries_removed": count}

    return _run(go, "clear_student_cache")


@_tool
def scan_students() -> dict:
    """Discover all students visible to the logged-in account across the discovery
    scope (the configured `EDUPAGE_SUBDOMAINS`, or every school when unset).
    Read-only; uses cached data to avoid redundant API calls.

    Returns:
        dict: {'students': [{name, student_id, class_id, subdomain}], 'total': n}.
        For a parent account: their linked children in each school; for a
        student/teacher: classmates. One entry per student per school.

    Notes:
        - `get_my_students` returns the same view for the active subdomain;
          `clear_student_cache` refreshes it.
    """
    def go():
        if not _clients:
            raise RuntimeError("Not logged in to any school. Set EDUPAGE_SUBDOMAINS (or call `login_all`) first.")
        discovered = []
        seen = set()
        for sub in _discovery_subdomains():
            block = _login_block_message(sub)
            if block:
                discovered.append({"subdomain": sub, "error": block})
                continue
            client = _clients[sub]
            try:
                students = _visible_students(client, sub)
                for student in students:
                    key = (student.person_id, sub)
                    if key in seen:
                        continue
                    seen.add(key)
                    discovered.append({
                        "name": _resolve_student_full_name(client, sub, student),
                        "student_id": student.person_id,
                        "class_id": getattr(student, "class_id", None),
                        "subdomain": sub,
                    })
            except Exception as e:  # noqa: BLE001
                discovered.append({"subdomain": sub, "error": f"{type(e).__name__}: {e}"})
        return {"students": discovered, "scanned": True, "total": len(discovered)}

    return _run(go, "scan_students")


def _visible_students(client, subdomain=None):
    """Students visible to the logged-in account: all students in school (parent),
    classmates (student/teacher). Uses cache to avoid redundant API calls."""
    sub = subdomain or ""
    return _get_students_cached(client, sub)


@_tool
def switch_to_parent(subdomain: str = None) -> dict:
    """Switch the session back to the parent account (parent accounts only).
    Writes: changes which account subsequent tools operate as.

    Args:
        subdomain: School whose session to restore (defaults to the active).

    Returns:
        dict: {'switched_to_parent': True, 'user_id': ...}.

    Notes:
        - Pair with `switch_to_student`; only relevant after a parent session
          was switched to a child.
    """
    def go():
        client = _require_client(subdomain)
        client.switch_to_parent()
        return {"switched_to_parent": True, "user_id": client.get_user_id()}

    return _run(go, "switch_to_parent")


# --------------------------------------------------------------------------
# Custom
# --------------------------------------------------------------------------
@_tool
def custom_request(url: str, method: str, data: str = "", headers: str = "{}", subdomain: str = None) -> dict:
    """Send a raw request to the Edupage server using the active session.
    Can perform writes depending on the endpoint — treat as write-capable.

    Args:
        url: Absolute URL, or a path like '/export/ajax_prevedene_meno.php'
            (resolved against `https://<subdomain>.edupage.org`).
        method: 'GET' or 'POST'.
        data: Request body (for POST).
        headers: JSON string of extra headers, e.g. '{"Accept": "application/json"}'.
        subdomain: School whose session to use (defaults to the active).

    Returns:
        dict: {'status_code': int, 'text': body}.

    Notes:
        - Low-level escape hatch for endpoints not covered by the dedicated
          tools — prefer those when available. Parse the returned text
          yourself; fields are not pre-serialized.
    """
    def go():
        client = _require_client(subdomain)
        hdrs = json.loads(headers) if headers else {}
        request_url = url
        parsed = urlparse(request_url)
        if not parsed.scheme:
            sub = _resolve_subdomain(subdomain)
            if not sub:
                raise RuntimeError("Cannot build absolute URL without a resolved subdomain.")
            path = request_url if request_url.startswith("/") else f"/{request_url}"
            request_url = f"https://{sub}.edupage.org{path}"
        resp = client.custom_request(request_url, method, data, hdrs)
        return {"status_code": resp.status_code, "text": resp.text}

    return _run(go, "custom_request")


# --------------------------------------------------------------------------
def main():
    if server is None:
        raise SystemExit("The 'mcp' python package is not installed.")
    if _MCP_PORT_ERROR:
        sys.stderr.write(f"{_MCP_PORT_ERROR}\n")
        raise SystemExit(1)
    if not _clients and EDUPAGE_USERNAME and EDUPAGE_PASSWORD:
        if EDUPAGE_SUBDOMAINS:
            subs = [s.strip() for s in EDUPAGE_SUBDOMAINS.split(",") if s.strip()]
            if subs:
                _autologin(subs)
        else:
            _autodiscover()
    transport = MCP_TRANSPORT.strip().lower()
    allowed = {"stdio", "sse", "streamable-http"}
    if transport not in allowed:
        sys.stderr.write(
            f"Error: invalid MCP_TRANSPORT '{MCP_TRANSPORT}'. Expected one of: {', '.join(sorted(allowed))}.\n"
        )
        raise SystemExit(1)
    server.run(transport=transport)


def _autodiscover():
    """Auto-discover a single school via login_auto when EDUPAGE_SUBDOMAINS is empty."""
    global _clients, _two_factor, _active_subdomain, _roles, _autologin_failures, _student_cache
    _autologin_failures = {}
    _student_cache = {}
    try:
        client = Edupage()
        tf = client.login_auto(EDUPAGE_USERNAME, EDUPAGE_PASSWORD)
        sub = client.subdomain or "auto"
        _clients[sub] = client
        _two_factor[sub] = tf
        _roles[sub] = _resolve_role(client)
        _active_subdomain = sub
        if tf is not None:
            _autologin_failures[sub] = "2FA required — call two_factor_finish"
    except Exception as e:  # noqa: BLE001
        _autologin_failures["portal"] = f"{type(e).__name__}: {e}"


def _autologin(subs):
    """Login to every school in `subs` with the shared EDUPAGE_USERNAME/PASSWORD.
    Tracks 2FA-pending and failed schools in _autologin_failures."""
    global _clients, _two_factor, _active_subdomain, _roles, _autologin_failures, _student_cache
    _autologin_failures = {}
    # Clear student cache since we're establishing fresh sessions
    _student_cache = {}
    for sub in subs:
        try:
            client = Edupage()
            tf = client.login(EDUPAGE_USERNAME, EDUPAGE_PASSWORD, sub)
            _clients[sub] = client
            _two_factor[sub] = tf
            _roles[sub] = _resolve_role(client)
            if tf is not None:
                _autologin_failures[sub] = "2FA required — call two_factor_finish"
        except Exception as e:  # noqa: BLE001
            _autologin_failures[sub] = f"{type(e).__name__}: {e}"
    if _clients:
        _active_subdomain = next(iter(_clients))


if __name__ == "__main__":
    main()

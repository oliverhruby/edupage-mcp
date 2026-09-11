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


def _discovery_subdomains():
    """Subdomains available to discovery/scanning tools.

    Restricted to the schools listed in `EDUPAGE_SUBDOMAINS` (comma-separated)
    whenever that variable is set, so auto-discovery never touches a school the
    user did not opt in to via SUBDOMAINS. When it is not set, every session
    in `_clients` is used. Callers must still surface login blocks for
    configured-but-unavailable schools instead of silently skipping them."""
    configured = [s.strip() for s in EDUPAGE_SUBDOMAINS.split(",") if s.strip()]
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
def login(username: str = None, password: str = None, subdomain: str = None) -> dict:
    """Log in to Edupage for a subdomain. If username/password/subdomain are
    omitted, env vars EDUPAGE_USERNAME, EDUPAGE_PASSWORD, EDUPAGE_SUBDOMAINS are used.
    Multiple schools are supported: each `login` call adds/replaces that subdomain's
    session (see `login_all`). If 2FA is enabled, returns instructions to call
    `two_factor_check_confirmed` / `two_factor_finish`.
    """
    global _clients, _two_factor, _active_subdomain

    def go():
        global _clients, _two_factor, _active_subdomain
        user = username or EDUPAGE_USERNAME
        pwd = password or EDUPAGE_PASSWORD
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
    """Log in to one or more schools using multiple subdomains in a single call.
    Pass comma-separated values: `subdomains="school1,school2"`,
    `usernames="u1,u2"`, `passwords="p1,p2"` (or pairs with one shared username/
    password). Uses env vars for anything not provided."""
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
def login_auto(username: str = None, password: str = None, subdomain: str = None) -> dict:
    """Log in to Edupage via the portal (auto-detect school). Optionally tag the
    resulting session with `subdomain` so multi-school tools can reference it."""
    global _clients, _two_factor, _active_subdomain

    def go():
        global _clients, _two_factor, _active_subdomain, _roles
        user = username or EDUPAGE_USERNAME
        pwd = password or EDUPAGE_PASSWORD
        if not (user and pwd):
            raise RuntimeError("username and password must be provided (or set as env vars).")
        client = Edupage()
        try:
            tf = client.login_auto(user, pwd)
        except Exception as e:  # noqa: BLE001
            _autologin_failures[subdomain or "portal"] = f"{type(e).__name__}: {e}"
            raise
        sub = subdomain or client.subdomain or "auto"
        _drop_student_cache(sub)
        _clients[sub] = client
        _two_factor[sub] = tf
        _roles[sub] = _resolve_role(client)
        _active_subdomain = sub
        return {"logged_in": True, "username": user, "subdomain": sub,
                "user_id": client.get_user_id(), "role": _roles[sub]}

    return _run(go, "login_auto")


@_tool
def login_from_session(session_id: str, subdomain: str, username: str) -> dict:
    """Create a logged-in Edupage instance from an existing PHPSESSID cookie."""
    global _clients, _active_subdomain

    def go():
        global _clients, _active_subdomain, _roles
        client = Edupage.from_session_id(session_id, subdomain, username)
        _drop_student_cache(subdomain)
        _clients[subdomain] = client
        _roles[subdomain] = _resolve_role(client)
        _active_subdomain = subdomain
        return {"logged_in": True, "username": username, "subdomain": subdomain,
                "role": _roles[subdomain]}

    return _run(go, "login_from_session")


@_tool
def two_factor_check_confirmed(subdomain: str = None) -> dict:
    """After a login that required 2FA, check whether the confirmation has been
    approved on a device. Returns True when safe to call `two_factor_finish`."""
    def go():
        sub = _resolve_subdomain(subdomain)
        _require_client(sub)
        tf = _two_factor.get(sub)
        if tf is None:
            raise RuntimeError(f"No pending 2FA login for '{sub}'. Call `login` first.")
        return {"confirmed": tf.is_confirmed(), "subdomain": sub}

    return _run(go, "two_factor check")


@_tool
def two_factor_finish(code: str = None, subdomain: str = None) -> dict:
    """Finish 2FA authentication. If `code` is provided it is used as an email/app
    code; otherwise the device-confirmation flow is used (call two_factor_check_confirmed first)."""
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
            tf.finish()
        _two_factor[sub] = None
        _drop_student_cache(sub)
        _roles[sub] = _resolve_role(client)
        return {"logged_in": True, "subdomain": sub, "user_id": client.get_user_id(),
                "role": _roles[sub]}

    return _run(go, "two_factor finish")


@_tool
def auth_status() -> dict:
    """Show login status for all configured subdomains and the active one."""
    sessions = {}
    for sub, client in _clients.items():
        sessions[sub] = {"logged_in": client.is_logged_in, "role": _roles.get(sub)}
    return {
        "logged_in_subdomains": sessions,
        "active_subdomain": _active_subdomain,
        "env_username_set": bool(EDUPAGE_USERNAME),
        "env_password_set": bool(EDUPAGE_PASSWORD),
        "env_subdomains_set": bool(EDUPAGE_SUBDOMAINS),
    }


@_tool
def user_id(subdomain: str = None) -> dict:
    """Return the logged-in user's Edupage user id."""
    def go():
        client = _require_client(subdomain)
        return {"user_id": client.get_user_id(), "subdomain": _resolve_subdomain(subdomain)}
    return _run(go, "user_id")


@_tool
def school_year(subdomain: str = None) -> dict:
    """Return the current school year (starting year)."""
    def go():
        client = _require_client(subdomain)
        return {"school_year": client.get_school_year(), "subdomain": _resolve_subdomain(subdomain)}
    return _run(go, "school_year")


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
    """Get the timetable for the logged-in user for a date (YYYY-MM-DD, default today)."""
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
    """Get a student's timetable by first/last name OR person_id.
    Without a `subdomain`, searches every school in the discovery scope (the
    configured `EDUPAGE_SUBDOMAINS`, or all logged-in schools when unset) and
    returns one result per school where the student is found — so a student
    attending multiple schools yields separate per-school timetables. If logged
    in as a parent, this switches to (and back from) the student account for the lookup.
    Returns the student's lessons plus which student/account/school was used."""
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


@_tool
def get_timetable(target_type: str, target_id: str, date_str: str = None, subdomain: str = None) -> dict:
    """Get the timetable for a teacher, student, class or classroom on a date.
    target_type: 'teacher' | 'student' | 'class' | 'classroom'."""
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        target = _resolve_target(client, target_type, target_id)
        try:
            tt = client.get_timetable(target, d)
        except (IndexError, KeyError, AttributeError, TypeError):
            tt = None
        base = {"target": f"{target_type}:{target_id}", "date": d.isoformat(),
                "subdomain": _resolve_subdomain(subdomain)}
        if tt is None:
            base["lessons"] = []
            return base
        base["lessons"] = [_serialize(ls) for ls in tt.lessons]
        return base

    return _run(go, "get_timetable")


@_tool
def get_next_ringing_time(date_time_str: str = None, subdomain: str = None) -> dict:
    """Get the type (break/lesson) and time of the next ringing for a given datetime
    (ISO, default now)."""
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
    grouped by weekday."""
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
def get_timetable_range(
    target_type: str,
    target_id: str,
    start_date: str,
    end_date: str,
    subdomain: str = None,
) -> dict:
    """Get timetable for a target (class, student, teacher, classroom) for every day
    between start_date and end_date (inclusive). Returns a dict keyed by date
    (YYYY‑MM‑DD) with each value being the result of `get_timetable` for that day.
    Days with no published data get an empty lessons list."""
    def go():
        client = _require_client(subdomain)
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if end < start:
            raise RuntimeError("end_date must be >= start_date")
        result: dict = {}
        cur = start
        while cur <= end:
            d = cur.isoformat()
            r = get_timetable(
                target_type=target_type,
                target_id=target_id,
                date_str=d,
                subdomain=subdomain,
            )
            result[d] = r
            cur += _dt.timedelta(days=1)
        return {"subdomain": _resolve_subdomain(subdomain), "range": result}

    return _run(go, "get_timetable_range")


@_tool
def get_periods(subdomain: str = None) -> dict:
    """Get the bell schedule (periods with start/end times) from the logged-in data."""
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
    """Get grades. Optionally filter by `year` (school year start) and `term`
    ('FIRST' or 'SECOND'). Returns list of grades (subject, teacher, percent, etc.)."""
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
def get_notifications(subdomain: str = None) -> dict:
    """Get the list of available timeline notifications (homework, tests, messages,
    grades, events...)."""
    def go():
        client = _require_client(subdomain)
        events = client.get_notifications()
        return {"subdomain": _resolve_subdomain(subdomain), "notifications": [_serialize(e) for e in events]}

    return _run(go, "get_notifications")


@_tool
def get_notification_history(date_from: str, subdomain: str = None) -> dict:
    """Get timeline notifications since a date (YYYY-MM-DD), including older ones."""
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_from)
        events = client.get_notification_history(d)
        return {"subdomain": _resolve_subdomain(subdomain), "notifications": [_serialize(e) for e in events]}

    return _run(go, "get_notification_history")


@_tool
def get_homework(subdomain: str = None) -> dict:
    """Get homework assignments from the recent timeline notifications."""
    def go():
        client = _require_client(subdomain)
        events = client.get_notifications()
        hw = [
            _serialize(e)
            for e in events
            if e.event_type in (EventType.HOMEWORK, EventType.HOMEWORK_STUDENT_STATE)
        ]
        return {"subdomain": _resolve_subdomain(subdomain), "homework": hw}

    return _run(go, "get_homework")


@_tool
def get_assignments(subdomain: str = None) -> dict:
    """Get all assignments (homework, tests, exams, projects) from the timeline."""
    def go():
        client = _require_client(subdomain)
        exam_types = {
            EventType.BIG_EXAM, EventType.HOMEWORK, EventType.ORAL_EXAM,
            EventType.PAPER, EventType.PROJECT_EXAM, EventType.SHORT_EXAM,
            EventType.TESTING, EventType.HOMEWORK_STUDENT_STATE,
            EventType.EXAM_ASSIGNMENT, EventType.EXAM_EVALUATION,
            EventType.TEST_RESULT,
        }
        events = client.get_notifications()
        result = [_serialize(e) for e in events if e.event_type in exam_types]
        return {"subdomain": _resolve_subdomain(subdomain), "assignments": result}

    return _run(go, "get_assignments")


@_tool
def get_absences(subdomain: str = None) -> dict:
    """Get the student's absence records from the timeline notifications."""
    def go():
        client = _require_client(subdomain)
        events = client.get_notifications()
        absence_types = {
            EventType.STUDENT_ABSENT, EventType.EXCUSED_LESSON, EventType.REPRESENTATION,
        }
        result = [_serialize(e) for e in events if e.event_type in absence_types]
        return {"subdomain": _resolve_subdomain(subdomain), "absences": result}

    return _run(go, "get_absences")


@_tool
def get_upcoming_events(subdomain: str = None) -> dict:
    """Get upcoming school events (trips, excursions, meetings, holidays...)."""
    def go():
        client = _require_client(subdomain)
        event_types = {
            EventType.EVENT, EventType.SCHOOL_EVENT, EventType.EXCURSION,
            EventType.SCHOOL_TRIP, EventType.PARENTS_EVENING, EventType.TEACHER_MEETING,
            EventType.CULTURE, EventType.SCHOOL_TEACHER_EVENT if hasattr(EventType, "SCHOOL_TEACHER_EVENT") else None,
            EventType.FREE_DAY, EventType.HOLIDAY, EventType.SHORT_HOLIDAY,
        }
        event_types.discard(None)
        events = client.get_notifications()
        result = [_serialize(e) for e in events if e.event_type in event_types]
        return {"subdomain": _resolve_subdomain(subdomain), "events": result}

    return _run(go, "get_upcoming_events")


@_tool
def get_news(subdomain: str = None) -> dict:
    """Get school news from the timeline notifications."""
    def go():
        client = _require_client(subdomain)
        events = client.get_notifications()
        result = [_serialize(e) for e in events if e.event_type == EventType.NEWS]
        return {"subdomain": _resolve_subdomain(subdomain), "news": result}

    return _run(go, "get_news")


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
    """Get substitution/timetable changes for a date (default today)."""
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        sub = _resolve_subdomain(subdomain)
        return {"date": d.isoformat(), "subdomain": sub, "changes": _get_changes_for(client, sub, d)}

    return _run(go, "get_timetable_changes")


@_tool
def get_missing_teachers(date_str: str = None, subdomain: str = None) -> dict:
    """Get teachers missing on a date (default today)."""
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


def _meals_payload(client, d, sub, include_breakfast=False, include_dinner=False):
    """Meal menu payload for a date: personal ordering endpoint first, then the
    school's public canteen widget. Returns a dict keyed by meal slot -> plain
    meal dict (or None). Pure function usable by both get_meals and
    get_day_summary."""
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
    if meals is None or not any(getattr(meals, m) for m in ("snack", "lunch", "afternoon_snack")):
        for fetch in (_fetch_canteen_menu, _fetch_novylistok):
            extra = fetch(client, d)
            if extra is None:
                continue
            meals_data = {k: extra[k] for k in ("snack", "lunch", "afternoon_snack")}
            if include_breakfast or include_dinner:
                meals_data["breakfast"] = extra.get("breakfast") if include_breakfast else None
                meals_data["dinner"] = extra.get("dinner") if include_dinner else None
            return meals_data
    meals_data = _serialize(meals) if meals is not None else None
    if include_breakfast or include_dinner:
        if meals_data is None:
            meals_data = {k: None for k in ("snack", "lunch", "afternoon_snack")}
        meals_data["breakfast"] = None if include_breakfast else meals_data.get("breakfast")
        meals_data["dinner"] = None if include_dinner else meals_data.get("dinner")
    return meals_data


@_tool
def get_meals(date_str: str = None, include_breakfast: bool = False,
              include_dinner: bool = False, subdomain: str = None) -> dict:
    """Get the meal menu (snack/lunch/afternoon snack) for a date (default today).

    Tries the personal meal-ordering endpoint first; when the school hasn't
    enabled it, falls back to the school's public canteen menu widget. By
    default only snack/lunch/afternoon_snack are returned; set
    include_breakfast / include_dinner to also include those extra meals
    (only available via the public widget)."""
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        sub = _resolve_subdomain(subdomain)
        return {"date": d.isoformat(), "subdomain": sub,
                "meals": _meals_payload(client, d, sub, include_breakfast, include_dinner)}

    return _run(go, "get_meals")


@_tool
def choose_meal(date_str: str, meal_type: str, number: int, subdomain: str = None) -> dict:
    """Order/choose a meal for a date. meal_type: 'snack'|'lunch'|'afternoon_snack'.
    number: 1-based menu choice among the chooseable menus."""
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
    """Cancel an ordered meal for a date. meal_type: 'snack'|'lunch'|'afternoon_snack'."""
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
    """Rate a meal (1-5 quality and quantity) for a date and meal type."""
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
def get_students(subdomain: str = None) -> dict:
    """Get all students in the logged-in user's class."""
    def go():
        client = _require_client(subdomain)
        return {"subdomain": _resolve_subdomain(subdomain),
                "students": [_serialize(s) for s in client.get_students() or []]}
    return _run(go, "get_students")


@_tool
def get_all_students(subdomain: str = None) -> dict:
    """Get a short list of all students in the school."""
    def go():
        client = _require_client(subdomain)
        return {"subdomain": _resolve_subdomain(subdomain),
                "students": [_serialize(s) for s in client.get_all_students() or []]}
    return _run(go, "get_all_students")


@_tool
def get_teachers(subdomain: str = None) -> dict:
    """Get all teachers in the school."""
    def go():
        client = _require_client(subdomain)
        return {"subdomain": _resolve_subdomain(subdomain),
                "teachers": [_serialize(t) for t in client.get_teachers() or []]}
    return _run(go, "get_teachers")


@_tool
def get_classes(subdomain: str = None) -> dict:
    """Get all classes in the school."""
    def go():
        client = _require_client(subdomain)
        return {"subdomain": _resolve_subdomain(subdomain),
                "classes": [_serialize(c) for c in client.get_classes() or []]}
    return _run(go, "get_classes")


@_tool
def get_classrooms(subdomain: str = None) -> dict:
    """Get all classrooms in the school."""
    def go():
        client = _require_client(subdomain)
        return {"subdomain": _resolve_subdomain(subdomain),
                "classrooms": [_serialize(c) for c in client.get_classrooms() or []]}
    return _run(go, "get_classrooms")


@_tool
def get_subjects(subdomain: str = None) -> dict:
    """Get all subjects in the school."""
    def go():
        client = _require_client(subdomain)
        return {"subdomain": _resolve_subdomain(subdomain),
                "subjects": [_serialize(s) for s in client.get_subjects() or []]}
    return _run(go, "get_subjects")


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------
@_tool
def send_message(recipient_id: str, body: str, subdomain: str = None) -> dict:
    """Send a message to a recipient. recipient_id is an edupage id like
    'Student123' or 'Teacher456' (see get_students/get_teachers)."""
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
    """Get students visible to the logged-in account: parent accounts see their
    linked children (parsed from the school homepage); student accounts see
    classmates. Uses cached data. Returns person_id, name, class_id — usable
    with switch_to_student and get_student_timetable."""
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
    """Switch to a student account (parent accounts only). Provide `student_id` (person_id)
    or `name` (first/last/full name)."""
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
    """Look up a student by first/last/full name using tiered matching.
    Without a `subdomain`, searches ALL logged-in schools and returns one result per
    school where the student is found. Returns student info with match confidence
    tiers (1=exact, 2=first name, 3=last name, 4=substring). Use student_id from
    results with get_student_timetable for unambiguous lookups."""
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
    """List all schools the server is logged into (from auto-discovery or login_all).
    Returns each subdomain with its login state, role (student/parent/teacher),
    2FA pending status, and user id."""
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
                "failed_logins": failed}

    return _run(go, "get_schools")


@_tool
def clear_student_cache(subdomain: str = None) -> dict:
    """Force refresh of cached student data. Call this after students are added/removed
    from a school, or if scan_students/find_student returns stale results.
    Without a subdomain, clears the cache for ALL schools."""
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
    For a parent account: their linked children in each school. For a student
    account: classmates in each school. Returns one entry per student per school,
    so a multi-school student appears with separate per-school records. Uses cached
    data to avoid redundant API calls."""
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
    """Switch back to the parent account (parent accounts only)."""
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
    method: 'GET'|'POST'. Returns status code and body text."""
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
            _autologin_failures[sub] = "2FA required — call two_factor_check_confirmed / two_factor_finish"
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
                _autologin_failures[sub] = "2FA required — call two_factor_check_confirmed / two_factor_finish"
        except Exception as e:  # noqa: BLE001
            _autologin_failures[sub] = f"{type(e).__name__}: {e}"
    if _clients:
        _active_subdomain = next(iter(_clients))


if __name__ == "__main__":
    main()

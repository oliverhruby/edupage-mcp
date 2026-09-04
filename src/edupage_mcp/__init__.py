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
import json
import os
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
from edupage_api.timeline import EventType, TimelineEvent
from edupage_api.timetables import Lesson, Timetable

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.auth.provider import AccessToken, TokenVerifier
except Exception:
    FastMCP = None
    AccessToken = None
    TokenVerifier = None

EDUPAGE_USERNAME = os.environ.get("EDUPAGE_USERNAME", "")
EDUPAGE_PASSWORD = os.environ.get("EDUPAGE_PASSWORD", "")
# Comma-separated list of schools to auto-login on startup (multi-school + automatic
# student discovery across all of them).
EDUPAGE_SUBDOMAINS = os.environ.get("EDUPAGE_SUBDOMAINS", "")
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
try:
    MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
except ValueError:
    MCP_PORT = 8000
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
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


def fail(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _resolve_subdomain(subdomain=None):
    if subdomain:
        return subdomain
    return _active_subdomain


def _require_client(subdomain=None):
    sub = _resolve_subdomain(subdomain)
    client = _clients.get(sub) if sub else None
    if client is None or not client.is_logged_in:
        raise RuntimeError(
            f"Not logged in for subdomain '{sub}'. Call `login` (or `login_all` for "
            "multiple schools) with that subdomain first."
        )
    return client


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


def _get_students_cached(client, subdomain):
    """Get students for a school, using cache to avoid redundant API calls.
    Returns list of student-like objects with .person_id, .name, .class_id."""
    role = _roles.get(subdomain, "student")
    cache_key = (subdomain, role)
    now = time.time()
    cached = _student_cache.get(cache_key)
    if cached and (now - cached["timestamp"]) < _STUDENT_CACHE_TTL:
        return cached["students"]
    if role == "parent":
        students = []
        # Prefer direct children for parent accounts (best names, smallest payload).
        try:
            students = client.get_my_children() or []
        except Exception:
            students = []
        # Some schools return short skeleton names only (e.g. initials) even for
        # parent views; enrich with class roster where possible.
        if students and not any(_has_full_name(s) for s in students):
            try:
                class_students = client.get_students() or []
            except Exception:
                class_students = []
            if class_students:
                by_id = {str(getattr(s, "person_id", "")): s for s in students}
                for s in class_students:
                    by_id[str(getattr(s, "person_id", ""))] = s
                students = list(by_id.values())
        # Fallback to school-wide roster if children endpoint is unavailable.
        if not students:
            try:
                students = client.get_all_students() or []
            except Exception:
                students = []
        if students and not any(_has_full_name(s) for s in students):
            try:
                class_students = client.get_students() or []
            except Exception:
                class_students = []
            if class_students:
                by_id = {str(getattr(s, "person_id", "")): s for s in students}
                for s in class_students:
                    by_id[str(getattr(s, "person_id", ""))] = s
                students = list(by_id.values())
        # Last fallback to generic visible students.
        if not students:
            students = client.get_students() or []
    else:
        students = client.get_students() or []
    _student_cache[cache_key] = {"students": students, "timestamp": now}
    return students


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
    server = FastMCP("edupage", host=MCP_HOST, port=MCP_PORT)
    token_verifier = None
    if MCP_API_KEY:
        token_verifier = _StaticApiKeyTokenVerifier(MCP_API_KEY)
    server = FastMCP("edupage", host=MCP_HOST, port=MCP_PORT, token_verifier=token_verifier)
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
        tf = client.login(user, pwd, sub)
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
                _clients[sub] = client
                _two_factor[sub] = tf
                _roles[sub] = _resolve_role(client)
                results.append({"subdomain": sub, "ok": True,
                                "user_id": client.get_user_id(),
                                "role": _roles[sub],
                                "two_factor_required": tf is not None})
            except Exception as e:  # noqa: BLE001
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
        tf = client.login_auto(user, pwd)
        sub = subdomain or client.subdomain or "auto"
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
      4. Short name match for parent accounts (e.g. 'Novak V.' matches 'Viktor Novak')

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
        # Tier 3b: initial-based short names (e.g. "Viktor Hruby" vs "VH")
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
    Returns None when the student is not found at this school. Role-aware: if the
    caller is a parent, temporarily switches to the student account. Uses cache."""
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
    sid = int(student.person_id)
    student_name = _student_name(student) or str(sid)
    role = _roles.get(sub, "student")
    is_parent = role == "parent"
    switch_id = sid
    if is_parent:
        # Parent sessions can expose different IDs for visible students vs
        # account-switching. Prefer get_child_id(name) when available.
        try:
            resolved = client.get_child_id(student_name)
            if resolved is not None:
                switch_id = int(resolved)
        except Exception:
            switch_id = sid
        try:
            client.switch_to_child(switch_id)
            tt = client.get_my_timetable(d)
            lessons = [_serialize(ls) for ls in tt.lessons] if tt else []
        except Exception:
            # Fallback path for schools/accounts where switch-to-child is not
            # available or IDs differ from roster identifiers.
            target = student
            if isinstance(student, EduStudentSkeleton):
                target = next(
                    (s for s in (client.get_students() or []) if str(getattr(s, "person_id", "")) == str(sid)),
                    student,
                )
            if isinstance(target, EduStudentSkeleton):
                tt = None
                lessons = []
            else:
                tt = client.get_timetable(target, d)
                lessons = [_serialize(ls) for ls in tt.lessons] if tt else []
        finally:
            try:
                client.switch_to_parent()
            except Exception:
                pass
    else:
        tt = client.get_my_timetable(d)
        lessons = [_serialize(ls) for ls in tt.lessons] if tt else []
    return {"student": student_name, "student_id": switch_id if is_parent else sid,
            "class_id": getattr(student, "class_id", None),
            "date": d.isoformat(), "subdomain": sub, "lessons": lessons}


@_tool
def get_student_timetable(name: str = None, student_id: str = None, date_str: str = None, subdomain: str = None) -> dict:
    """Get a student's timetable by first/last name OR person_id.
    Without a `subdomain`, searches ALL logged-in schools and returns one result per
    school where the student is found — so a student attending multiple schools (e.g.
    Tamara at iprskola + cvcmalacky) yields separate per-school timetables. If logged
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
        schools = list(_clients.keys())
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
@_tool
def get_timetable_changes(date_str: str = None, subdomain: str = None) -> dict:
    """Get substitution/timetable changes for a date (default today)."""
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        sub = _resolve_subdomain(subdomain)
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
        if changes is None:
            return {"date": d.isoformat(), "subdomain": _resolve_subdomain(subdomain), "changes": []}
        return {"date": d.isoformat(), "subdomain": _resolve_subdomain(subdomain),
                "changes": [_serialize(c) for c in changes]}

    return _run(go, "get_timetable_changes")


@_tool
def get_missing_teachers(date_str: str = None, subdomain: str = None) -> dict:
    """Get teachers missing on a date (default today)."""
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        teachers = client.get_missing_teachers(d)
        return {"date": d.isoformat(), "subdomain": _resolve_subdomain(subdomain),
                "teachers": [_serialize(t) for t in teachers or []]}

    return _run(go, "get_missing_teachers")


# --------------------------------------------------------------------------
# Meals
# --------------------------------------------------------------------------
@_tool
def get_meals(date_str: str = None, subdomain: str = None) -> dict:
    """Get the meal menu (snack/lunch/afternoon snack) for a date (default today)."""
    def go():
        client = _require_client(subdomain)
        d = _parse_date(date_str)
        try:
            meals = client.get_meals(d)
        except (edupage_exceptions.InvalidMealsData, IndexError, AttributeError, KeyError, TypeError):
            meals = Meals(None, None, None)
        except edupage_exceptions.ExpiredSessionException:
            sub = _resolve_subdomain(subdomain)
            if _relogin_subdomain(sub):
                client = _require_client(sub)
                meals = client.get_meals(d)
            else:
                raise
        return {"date": d.isoformat(), "subdomain": _resolve_subdomain(subdomain), "meals": _serialize(meals)}

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
    """Get students visible to the logged-in account: parent accounts see all
    students in the school; student accounts see classmates. Uses cached data.
    Returns person_id, name, class_id — usable with switch_to_student and
    get_student_timetable."""
    def go():
        client = _require_client(subdomain)
        sub = _resolve_subdomain(subdomain)
        role = _roles.get(sub, "student")
        students = _get_students_cached(client, sub)
        if role == "parent":
            serialized = []
            for s in students:
                serialized.append(_serialize(s))
            return {"subdomain": sub, "students": serialized}
        classmates = []
        for s in students:
            classmates.append({
                "person_id": s.person_id,
                "name": _student_name(s),
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
        schools = list(_clients.keys())
        if not schools:
            raise RuntimeError("Not logged in to any school. Set EDUPAGE_SUBDOMAINS (or call `login_all`) first.")
        all_results = []
        for sub in schools:
            client = _clients[sub]
            if client is None or not client.is_logged_in:
                continue
            matches = _find_student_all(client, name, sub)
            for m in matches:
                all_results.append({
                    "name": m["name"], "student_id": m["student_id"],
                    "class_id": m["class_id"], "subdomain": m["subdomain"],
                    "tier": m["tier"], "confidence": m["confidence"],
                })
        if not all_results:
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
    """Discover all students visible to the logged-in account across every school.
    For a parent account: all students in each school. For a student account:
    classmates in each school. Returns one entry per student per school, so a
    multi-school student (e.g. Tamara at iprskola + cvcmalacky) appears with
    separate per-school records. Uses cached data to avoid redundant API calls."""
    def go():
        if not _clients:
            raise RuntimeError("Not logged in to any school. Set EDUPAGE_SUBDOMAINS (or call `login_all`) first.")
        discovered = []
        seen = set()
        for sub in _clients:
            client = _clients[sub]
            if not (client and client.is_logged_in):
                continue
            try:
                students = _visible_students(client, sub)
                for student in students:
                    key = (student.person_id, sub)
                    if key in seen:
                        continue
                    seen.add(key)
                    discovered.append({
                        "name": _student_name(student),
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
        sys.exit(1)
    if transport in ("sse", "streamable-http") and MCP_HOST == "0.0.0.0" and not MCP_API_KEY:
        sys.stderr.write("Error: MCP_API_KEY must be set when binding to 0.0.0.0 for HTTP transport.\n")
        sys.exit(1)
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

"""AI Grader web app for teachers (Flask backend)."""
import os

# Sanitize the proxy bypass list BEFORE anything imports httpx (via the OpenAI
# SDK). Some sandboxes inject bracketed IPv6 entries like "[::1]" into
# NO_PROXY/no_proxy, which httpx cannot parse and fails with
# "Invalid port: ':1]'" on every API call. Keep a minimal, parseable list;
# api.openai.com still goes through the egress proxy as intended.
for _pv in ("NO_PROXY", "no_proxy"):
    _vv = os.environ.get(_pv, "")
    if "[::1]" in _vv or "*" in _vv:
        os.environ[_pv] = "localhost,127.0.0.1,::1"

import csv
import hashlib
import hmac
import io
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for

import db
import grader

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # max 32MB per request

# Trust the last N proxy hops for X-Forwarded-For (Render = 1). With
# ProxyFix, request.remote_addr is the real client IP; without it,
# access_route[0] is client-spoofable and per-IP rate limits are useless.
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))
if TRUSTED_PROXY_HOPS > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXY_HOPS)


def _client_ip():
    """Client IP for rate limiting (proxy-aware via ProxyFix)."""
    return request.remote_addr or "unknown"

# Sessions: must be set so login cookies work across gunicorn workers and
# survive restarts. Fail closed: no silent random fallback.
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key or len(app.secret_key) < 32:
    raise RuntimeError("FLASK_SECRET_KEY (>=32 chars) must be set")

# Cookie hardening (H3).
app.config.update(
    SESSION_COOKIE_SECURE=True,      # Render serves HTTPS
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

# Shared login. Three roles, one form:
#   GRADER_PASSWORD -> tester (uses the app, no /settings)
#   ADMIN_PASSWORD  -> admin (full access incl. /settings and public monitoring)
#   PUBLIC_PASSWORD -> public (isolated demo data, rate-limited, no /settings)
# Fail closed: no silent defaults anywhere (empty password must never match).
# The admin and tester roles have separate usernames (ADMIN_USER vs GRADER_USER)
# so the two roles are never confused.
# Usernames are canonicalized to lowercase once here: env-role logins compare
# case-insensitively, matching how registered teacher usernames are stored.
# (Found by gpt-5.6 review 2026-10-10: case-only collisions like
# ADMIN_USER=Teacher vs GRADER_USER=teacher slipped past the checks.)
GRADER_USER = (os.environ.get("GRADER_USER", "teacher") or "").strip().lower()
GRADER_PASSWORD = os.environ.get("GRADER_PASSWORD")
ADMIN_USER = (os.environ.get("ADMIN_USER", "admin") or "").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
PUBLIC_USER = (os.environ.get("PUBLIC_USER", "public") or "").strip().lower()
PUBLIC_PASSWORD = os.environ.get("PUBLIC_PASSWORD", "")
if not GRADER_USER:
    raise RuntimeError("GRADER_USER must not be empty")
if not ADMIN_USER:
    raise RuntimeError("ADMIN_USER must not be empty")
if not GRADER_PASSWORD or len(GRADER_PASSWORD) < 12:
    raise RuntimeError("GRADER_PASSWORD (>=12 chars) must be set")
if not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 12:
    raise RuntimeError("ADMIN_PASSWORD (>=12 chars) must be set")
# Identical passwords would silently promote testers to admin (admin check
# runs first). Fail closed instead. Found by gpt-5.6 review 2026-10-07.
if GRADER_PASSWORD == ADMIN_PASSWORD:
    raise RuntimeError("GRADER_PASSWORD and ADMIN_PASSWORD must differ")
# The three env-role usernames must never collide with each other, or one
# login name could resolve to two roles. Checked unconditionally: the public
# account is optional, but the admin/tester split must hold regardless.
# (Found by gpt-5.6 review 2026-10-10: these used to live inside the
# PUBLIC_PASSWORD block and were skipped when the demo account was disabled.)
if ADMIN_USER == GRADER_USER:
    raise RuntimeError("ADMIN_USER must differ from GRADER_USER")
if ADMIN_USER == PUBLIC_USER:
    raise RuntimeError("ADMIN_USER must differ from PUBLIC_USER")
if PUBLIC_USER == GRADER_USER:
    raise RuntimeError("PUBLIC_USER must differ from GRADER_USER")
# The public demo account is optional: empty PUBLIC_PASSWORD disables it.
# The password may be short/simple (it's a shared demo login); the real
# protection is data isolation + rate limits, not password strength.
# It must still differ from both other passwords, otherwise a public login
# could silently escalate to tester/admin.
if PUBLIC_PASSWORD:
    if PUBLIC_PASSWORD in (GRADER_PASSWORD, ADMIN_PASSWORD):
        raise RuntimeError("PUBLIC_PASSWORD must differ from GRADER_PASSWORD and ADMIN_PASSWORD")
    # An empty public username with the account enabled would be silently
    # unusable (or worse); fail closed. Found by gpt-5.6 review 2026-10-10.
    if not PUBLIC_USER:
        raise RuntimeError("PUBLIC_USER must not be empty when PUBLIC_PASSWORD is set")

# Auth version: changing any password invalidates all sessions.
# HMAC-keyed with the server secret so the cookie value (signed, not encrypted)
# doesn't leak a crackable password hash.
def _auth_version_material():
    """Canonical serialization of the login passwords for AUTH_VERSION.

    JSON — not f"{a}:{b}:{c}" — so passwords containing ':' can never make
    two different triples serialize identically. A password change must always
    invalidate existing sessions.
    """
    return json.dumps(
        {"grader": GRADER_PASSWORD, "admin": ADMIN_PASSWORD,
         "public": PUBLIC_PASSWORD},
        sort_keys=True, separators=(",", ":")).encode()


AUTH_VERSION = hmac.new(
    app.secret_key.encode(),
    _auth_version_material(),
    hashlib.sha256).hexdigest()[:32]

# Public demo account limits (only apply to the "public" role).
# All tunable via environment.
PUBLIC_MAX_RUBRIC_JOBS_PER_DAY = int(os.environ.get("PUBLIC_MAX_RUBRIC_JOBS_PER_DAY", "3"))
PUBLIC_MAX_GRADES_PER_DAY = int(os.environ.get("PUBLIC_MAX_GRADES_PER_DAY", "20"))
PUBLIC_MAX_RETRIES_PER_DAY = int(os.environ.get("PUBLIC_MAX_RETRIES_PER_DAY", "10"))
# One photo per question for the demo account (token protection).
PUBLIC_MAX_RUBRIC_IMAGES = int(os.environ.get("PUBLIC_MAX_RUBRIC_IMAGES", "1"))
PUBLIC_MAX_GRADE_IMAGES = int(os.environ.get("PUBLIC_MAX_GRADE_IMAGES", "4"))
# Lifetime cap on questions the demo account may create/fix via rubric
# generation + retries. 0 disables the lifetime cap.
PUBLIC_MAX_QUESTIONS_LIFETIME = int(os.environ.get("PUBLIC_MAX_QUESTIONS_LIFETIME", "100"))
# Per-day token cap for the public role specifically (in addition to the
# global DAILY_TOKEN_BUDGET). 0 disables the per-role cap.
PUBLIC_DAILY_TOKEN_BUDGET = int(os.environ.get("PUBLIC_DAILY_TOKEN_BUDGET", "500000"))
# Public demo text-input cap (characters) for retry/regenerate: each
# operation's prompt must stay bounded, or a single call with a huge
# question_text could blow past the daily token budget.
PUBLIC_MAX_TEXT_CHARS = int(os.environ.get("PUBLIC_MAX_TEXT_CHARS", "4000"))
# Public demo stored-image caps per assignment (storage + token protection):
# reference photos (answer keys) and rubric example photos. The reference
# default is deliberately below PUBLIC_MAX_GRADE_IMAGES: with 4 reference
# photos and a 4-photo grading cap, no paper photo would fit and the
# assignment could never be graded.
PUBLIC_MAX_REFERENCE_IMAGES = int(os.environ.get("PUBLIC_MAX_REFERENCE_IMAGES", "3"))
PUBLIC_MAX_EXAMPLE_IMAGES = int(os.environ.get("PUBLIC_MAX_EXAMPLE_IMAGES", "4"))
# Public demo rubric size cap (characters, rubric_text + rubric_json): rubrics
# are saved by the user and sent to the model on every grading, so an
# uncapped stored rubric would bypass the per-request text caps.
PUBLIC_MAX_RUBRIC_CHARS = int(os.environ.get("PUBLIC_MAX_RUBRIC_CHARS", "20000"))
# Public demo data-volume caps (storage protection): strangers must not fill
# the database with unlimited classes/assignments.
PUBLIC_MAX_CLASSES = int(os.environ.get("PUBLIC_MAX_CLASSES", "10"))
PUBLIC_MAX_ASSIGNMENTS = int(os.environ.get("PUBLIC_MAX_ASSIGNMENTS", "20"))

# Log every OpenAI call for owner cost visibility.
grader.usage_logger = db.log_api_usage

# Daily token budget (M1): refuse new API calls once exceeded.
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "2000000"))


# ---------------------------------------------------------------------------
# Registered teachers: self-service signup, per-teacher data partitions,
# tiered rate limits controlled by the admin.
# ---------------------------------------------------------------------------

# Limit keys (shared by the public demo role and teacher tiers).
_LIMIT_KEYS = (
    "max_rubric_jobs_per_day",
    "max_grades_per_day",
    "max_retries_per_day",
    "daily_token_budget",
    "max_questions_lifetime",
    "max_text_chars",
    "max_rubric_chars",
    "max_reference_images",
    "max_example_images",
    "max_rubric_images",
    "max_grade_images",
    "max_classes",
    "max_assignments",
)

# Tier presets. "free" mirrors the public demo defaults. A 0 disables the
# lifetime-question and daily-token caps (matching the PUBLIC_* convention);
# for the count caps, 0 means "none allowed". The admin assigns a tier per
# teacher and may additionally store per-teacher overrides.
TEACHER_TIERS = {
    "free": {
        "max_rubric_jobs_per_day": 3,
        "max_grades_per_day": 20,
        "max_retries_per_day": 10,
        "daily_token_budget": 500000,
        "max_questions_lifetime": 100,
        "max_text_chars": 4000,
        "max_rubric_chars": 20000,
        "max_reference_images": 3,
        "max_example_images": 4,
        "max_rubric_images": 1,
        "max_grade_images": 4,
        "max_classes": 10,
        "max_assignments": 20,
    },
    "standard": {
        "max_rubric_jobs_per_day": 10,
        "max_grades_per_day": 100,
        "max_retries_per_day": 30,
        "daily_token_budget": 1000000,
        "max_questions_lifetime": 500,
        "max_text_chars": 8000,
        "max_rubric_chars": 50000,
        "max_reference_images": 5,
        "max_example_images": 8,
        "max_rubric_images": 2,
        "max_grade_images": 8,
        "max_classes": 30,
        "max_assignments": 60,
    },
    "pro": {
        "max_rubric_jobs_per_day": 30,
        "max_grades_per_day": 500,
        "max_retries_per_day": 100,
        "daily_token_budget": 3000000,
        "max_questions_lifetime": 0,
        "max_text_chars": 16000,
        "max_rubric_chars": 100000,
        "max_reference_images": 10,
        "max_example_images": 12,
        "max_rubric_images": 4,
        "max_grade_images": 12,
        "max_classes": 100,
        "max_assignments": 200,
    },
}
DEFAULT_TEACHER_TIER = "free"

# Usernames that can never be registered (they address the env-password roles).
# Built from the configured login names, so a custom ADMIN_USER/GRADER_USER/
# PUBLIC_USER cannot be shadowed by a teacher account either.
def _reserved_usernames():
    return {"teacher", "admin", "public",
            (ADMIN_USER or "").strip().lower(),
            (GRADER_USER or "").strip().lower(),
            (PUBLIC_USER or "").strip().lower()} - {""}

# Registration anti-spam: signups per client IP per day, and global per day.
REGISTER_MAX_PER_IP_PER_DAY = int(os.environ.get("REGISTER_MAX_PER_IP_PER_DAY", "10"))
REGISTER_MAX_GLOBAL_PER_DAY = int(os.environ.get("REGISTER_MAX_GLOBAL_PER_DAY", "200"))
# Teacher-login brute-force guard: attempts per client IP per day.
REGISTER_MAX_LOGIN_PER_IP_PER_DAY = int(os.environ.get("REGISTER_MAX_LOGIN_PER_IP_PER_DAY", "100"))


def _teacher_limits_for(user_id):
    """Effective limits dict for a teacher: tier defaults + per-teacher overrides.

    Fail closed: unknown tier, missing user, or corrupt overrides fall back to
    the free tier.
    """
    tier = TEACHER_TIERS.get(DEFAULT_TEACHER_TIER, {})
    try:
        user = db.get_user_by_id(user_id) if user_id else None
    except Exception:
        user = None
    if user:
        tier = TEACHER_TIERS.get(user.get("tier") or "", tier) or tier
        try:
            overrides = json.loads(user.get("limits_json") or "{}")
        except Exception:
            overrides = {}
        if isinstance(overrides, dict):
            merged = dict(tier)
            for k, v in overrides.items():
                if k in _LIMIT_KEYS:
                    try:
                        merged[k] = int(v)
                    except (TypeError, ValueError):
                        pass
            return merged
    return dict(tier)


def _limits_for_role(role):
    """Limits dict for an owner/role string, or None when unlimited.

    "public" -> the PUBLIC_* demo constants (read live so tests can patch
    them). "teacher:<username>" -> that teacher's tier limits. Anything else
    (owner roles) -> None. Session-free: safe in worker threads.
    """
    if role == "public":
        return {
            "max_rubric_jobs_per_day": PUBLIC_MAX_RUBRIC_JOBS_PER_DAY,
            "max_grades_per_day": PUBLIC_MAX_GRADES_PER_DAY,
            "max_retries_per_day": PUBLIC_MAX_RETRIES_PER_DAY,
            "daily_token_budget": PUBLIC_DAILY_TOKEN_BUDGET,
            "max_questions_lifetime": PUBLIC_MAX_QUESTIONS_LIFETIME,
            "max_text_chars": PUBLIC_MAX_TEXT_CHARS,
            "max_rubric_chars": PUBLIC_MAX_RUBRIC_CHARS,
            "max_reference_images": PUBLIC_MAX_REFERENCE_IMAGES,
            "max_example_images": PUBLIC_MAX_EXAMPLE_IMAGES,
            "max_rubric_images": PUBLIC_MAX_RUBRIC_IMAGES,
            "max_grade_images": PUBLIC_MAX_GRADE_IMAGES,
            "max_classes": PUBLIC_MAX_CLASSES,
            "max_assignments": PUBLIC_MAX_ASSIGNMENTS,
        }
    if role and role.startswith("teacher:"):
        try:
            return _teacher_limits_for(int(role[len("teacher:"):]))
        except (TypeError, ValueError):
            return _teacher_limits_for(0)
    return None


def _is_teacher():
    """True when the current session is a registered teacher."""
    return session.get("auth_v") == AUTH_VERSION and session.get("role") == "teacher"


def _limited_limits():
    """Limits dict for the current rate-limited role, else None.

    Covers the public demo role and registered teachers. Owner roles
    (tester/admin) are unlimited.
    """
    if session.get("auth_v") != AUTH_VERSION:
        return None
    role = session.get("role")
    if role == "public":
        return _limits_for_role("public")
    if role == "teacher":
        return _limits_for_role(f"teacher:{session.get('user_id') or 0}")
    return None


def _limit_label():
    """Human label for limit errors: 'Demo' for the public role, 'Plan' for teachers."""
    return "Demo" if _is_public() else "Plan"


def _budget_ok():
    """Token-budget gate, checked before every OpenAI call.

    Enforces the global DAILY_TOKEN_BUDGET for everyone, and additionally the
    per-role daily token budget for rate-limited roles (public demo,
    registered teachers) on each call — not just at route entry — so
    multi-call operations (rubric builds, retries) and any future route cannot
    overshoot it by more than the in-flight calls.

    Uses the ContextVar role, never `session`: this runs in background and
    worker threads that have no request context. The role is propagated into
    pool workers by grader._submit_with_context.
    """
    start_of_day = time.time() - (time.time() % 86400)
    s = db.get_api_usage_summary(since_ts=start_of_day)
    if (s["prompt_tokens"] + s["completion_tokens"]) >= DAILY_TOKEN_BUDGET:
        return False
    role = grader._get_current_role()
    lim = _limits_for_role(role)
    if lim is not None and lim["daily_token_budget"] > 0:
        used = db.get_api_usage_summary_for_role(role)
        if (used["prompt_tokens"] + used["completion_tokens"]) >= lim["daily_token_budget"]:
            return False
    return True


grader.budget_checker = _budget_ok

# The public demo's daily token budget must sit well below the global daily
# budget: strangers must never be able to exhaust the owner's own grading
# budget (financial-adjacent denial of service). 2x headroom minimum.
# Fail closed: with the public account enabled, neither budget may be
# disabled (0) — an unbounded demo account is not a supported configuration.
if PUBLIC_PASSWORD:
    if DAILY_TOKEN_BUDGET <= 0:
        raise RuntimeError(
            "DAILY_TOKEN_BUDGET must be positive when the public account is enabled")
    if PUBLIC_DAILY_TOKEN_BUDGET <= 0:
        raise RuntimeError(
            "PUBLIC_DAILY_TOKEN_BUDGET must be positive when the public account is enabled")
    if PUBLIC_DAILY_TOKEN_BUDGET * 2 > DAILY_TOKEN_BUDGET:
        raise RuntimeError(
            "PUBLIC_DAILY_TOKEN_BUDGET must be at most half of DAILY_TOKEN_BUDGET")
    # The reference-photo default must leave room for at least one paper photo
    # under the grading cap; otherwise a maxed-out assignment is ungradable.
    if PUBLIC_MAX_REFERENCE_IMAGES >= PUBLIC_MAX_GRADE_IMAGES:
        raise RuntimeError(
            "PUBLIC_MAX_REFERENCE_IMAGES must be below PUBLIC_MAX_GRADE_IMAGES")
    # The demo role's model must be a cheap non-reasoning model: an
    # OPENAI_MODEL=o3 (or PUBLIC_MODEL=o3) override would otherwise give
    # strangers unbounded reasoning spend against the demo token budget.
    if grader.PUBLIC_MODEL not in grader.PUBLIC_MODEL_ALLOWLIST:
        raise RuntimeError(
            f"PUBLIC_MODEL must be one of {sorted(grader.PUBLIC_MODEL_ALLOWLIST)} "
            "when the public account is enabled")

db.init_db()

# An env-role username must never match an already-registered teacher: env
# roles are checked before registered teachers at login, so one username
# resolving to two identities would confuse the roles. Registration blocks
# these names going forward; this guards the pre-existing-data case.
# Found by gpt-5.6 review 2026-10-10.
for _env_role_name in (ADMIN_USER, GRADER_USER, PUBLIC_USER):
    # Env-role names are already canonical lowercase, matching how teacher
    # usernames are stored.
    if db.get_user_by_username(_env_role_name):
        raise RuntimeError(
            f"Configured login name {_env_role_name!r} collides with a "
            "registered teacher account; rename one of them.")


@app.before_request
def _require_login():
    if request.path == "/login" or request.path == "/register" \
            or request.path.startswith("/static/"):
        return None
    if session.get("auth_v") != AUTH_VERSION:
        session.clear()
        # API/AJAX callers get JSON 401, browsers get the login page (M4).
        if request.path.startswith("/api/") or request.headers.get(
                "X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": False, "error": "auth_required"}), 401
        return redirect(url_for("login_page"))
    # A disabled teacher is locked out immediately, on every request.
    # The session is also bound to the immutable user id: if the row's id
    # ever stops matching (e.g. username recycled), the session dies.
    if session.get("role") == "teacher":
        try:
            user = db.get_user_by_username(session.get("username") or "")
        except Exception:
            user = None
        if not user or user.get("disabled") or user["id"] != session.get("user_id"):
            session.clear()
            if request.path.startswith("/api/") or request.headers.get(
                    "X-Requested-With") == "XMLHttpRequest":
                return jsonify({"ok": False, "error": "auth_required"}), 401
            return redirect(url_for("login_page"))
    # Attribute API token usage to the caller's role for monitoring.
    # Background job threads set their own role at start (see _run_solve_job);
    # ContextVar keeps threads isolated.
    grader._set_current_role(_data_owner())
    # Reset the operation label: worker threads serve requests sequentially
    # and the ContextVar persists across them — without this reset, a retry
    # handled right after a grading on the same thread would be logged under
    # the wrong operation on the admin monitor page.
    grader._set_current_operation("general")


def _is_admin():
    return session.get("auth_v") == AUTH_VERSION and session.get("role") == "admin"


def _is_public():
    """True when the current session is the rate-limited public demo role."""
    return session.get("auth_v") == AUTH_VERSION and session.get("role") == "public"


def _data_owner():
    """Data partition for the current session.

    'public' for the public demo role (fully isolated sandbox),
    'teacher:<username>' for registered teachers (each teacher's own
    dataset), 'private' for the owner's tester/admin roles.

    Fail closed: any stale session or unknown role aborts instead of falling
    through to the owner's partition.
    """
    if session.get("auth_v") != AUTH_VERSION:
        abort(401)
    role = session.get("role")
    if role == "public":
        return "public"
    if role == "teacher":
        # Partitions are keyed by the immutable user id, never the username:
        # a recycled username must never inherit another teacher's dataset.
        # _require_login already verified this id against the users table.
        user_id = session.get("user_id")
        if isinstance(user_id, int) and user_id > 0:
            return f"teacher:{user_id}"
        abort(403)
    if role in ("tester", "admin"):
        return "private"
    abort(403)


def _limited_text_too_long(*fields):
    """True when a rate-limited role's text input exceeds its character cap."""
    lim = _limited_limits()
    return lim is not None and any(len(f or "") > lim["max_text_chars"]
                                   for f in fields)


def _limited_rubric_too_long(a):
    """True when a rate-limited role's stored rubric exceeds its size cap.

    Stored rubrics are sent to the model on every grading, so the size must
    be bounded both when saved and when used.
    """
    lim = _limited_limits()
    return lim is not None and (
        len(a.get("rubric_text") or "") + len(a.get("rubric_json") or "")
    ) > lim["max_rubric_chars"]


def _require_class(class_id):
    """Load a class or abort: 404 if missing, 403 if in another partition."""
    c = db.get_class(class_id)
    if not c:
        abort(404)
    if c.get("owner", "private") != _data_owner():
        abort(403)
    return c


def _require_assignment(assignment_id):
    """Load an assignment or abort: 404 if missing, 403 if cross-partition."""
    a = db.get_assignment(assignment_id)
    if not a:
        abort(404)
    _require_class(a["class_id"])  # 403 on cross-partition access
    return a


def _require_grade(grade_id):
    """Load a grade or abort: 404 if missing, 403 if cross-partition."""
    g = db.get_grade(grade_id)
    if not g:
        abort(404)
    _require_assignment(g["assignment_id"])  # 403 on cross-partition access
    return g


def _limited_rate_limit_exceeded(op):
    """Check + record one hit of a daily rate limit for rate-limited roles.

    Returns an error string when the limit is hit, else None. No-op for
    unlimited (owner) roles. The counter key is the data-owner partition, so
    each teacher gets their own counters.
    """
    lim = _limited_limits()
    if lim is None:
        return None
    key = {"rubric_job": "max_rubric_jobs_per_day",
           "grade": "max_grades_per_day",
           "retry": "max_retries_per_day"}[op]
    if not db.check_rate_limit(_data_owner(), op, lim[key]):
        return (f"{_limit_label()} limit reached: {lim[key]} {op.replace('_', ' ')}s "
                "per day. Please try again tomorrow.")
    return None


def _limited_token_budget_exceeded():
    """True when the rate-limited role burned its daily token budget (0 = disabled)."""
    lim = _limited_limits()
    if lim is None or lim["daily_token_budget"] <= 0:
        return False
    day = db._rate_limit_day()
    used = db.get_api_usage_summary_for_role(_data_owner(), day)
    return (used["prompt_tokens"] + used["completion_tokens"]) >= lim["daily_token_budget"]


def _limited_lifetime_cap_error():
    """Error string when the rate-limited role hit its lifetime question cap.

    Returns None for unlimited roles, when the cap is disabled (0), or when
    under the cap.
    """
    lim = _limited_limits()
    if lim is None or lim["max_questions_lifetime"] <= 0:
        return None
    used = db.get_lifetime_usage(_data_owner(), "questions")
    if used >= lim["max_questions_lifetime"]:
        return (f"{_limit_label()} limit reached: {lim['max_questions_lifetime']} questions "
                "total for this account. No more questions can be created or fixed.")
    return None


@app.route("/login", methods=["GET", "POST"])
def login_page():
    err = ""
    if request.method == "POST":
        session.clear()  # prevent session fixation (L2)
        pw = (request.form.get("password") or "").encode()
        user = request.form.get("username") or ""
        # Env-role usernames are canonical lowercase; compare in that space
        # so "Admin" and "admin" resolve to the same role.
        user_lc = user.strip().lower()
        # Never match an empty password (fail closed).
        def _match(secret):
            s = (secret or "").encode()
            return bool(pw) and bool(s) and hmac.compare_digest(pw, s)
        # Constant-time comparison (L1). Admin check first (see fail-closed
        # note above); public check last so it can never shadow the others.
        # Registered teachers are checked after the env-password roles: their
        # usernames can never be "teacher"/"admin"/"public" (reserved at
        # signup), so there is no shadowing either way.
        if user_lc == ADMIN_USER and _match(ADMIN_PASSWORD):
            session["auth_v"] = AUTH_VERSION
            session["role"] = "admin"
            session.permanent = True
            return redirect(url_for("index"))
        if user_lc == GRADER_USER and _match(GRADER_PASSWORD):
            session["auth_v"] = AUTH_VERSION
            session["role"] = "tester"
            session.permanent = True
            return redirect(url_for("index"))
        # Legacy admin combo: before 2026-10-10 the admin logged in as the
        # tester username + ADMIN_PASSWORD. Point at the new username instead
        # of a bare "wrong password" so upgrades don't silently lock admins out.
        if user_lc == GRADER_USER and _match(ADMIN_PASSWORD):
            err = (f"Admin login now uses username '{ADMIN_USER}'. "
                   "Please log in again with the admin username.")
            return render_template("login.html", error=err)
        if PUBLIC_PASSWORD and user_lc == PUBLIC_USER and _match(PUBLIC_PASSWORD):
            session["auth_v"] = AUTH_VERSION
            session["role"] = "public"
            session.permanent = True
            return redirect(url_for("index"))
        teacher = db.get_user_by_username(user.strip().lower())
        if teacher and not teacher.get("disabled"):
            # Brute-force guard: cap teacher-login attempts per client IP.
            # (Env-password roles keep their existing behavior.)
            login_ip = _client_ip()
            if not db.check_rate_limit(f"login:{login_ip}", "teacher_attempt",
                                       REGISTER_MAX_LOGIN_PER_IP_PER_DAY):
                err = "Too many login attempts from this address today. Please try again tomorrow."
            else:
                from werkzeug.security import check_password_hash as _check
                if _check(teacher["password_hash"], pw.decode("utf-8", "replace")):
                    session["auth_v"] = AUTH_VERSION
                    session["role"] = "teacher"
                    session["user_id"] = teacher["id"]
                    session["username"] = teacher["username"]
                    session.permanent = True
                    return redirect(url_for("index"))
                err = "Wrong username or password."
        else:
            err = "Wrong username or password."
    return render_template("login.html", error=err)


@app.route("/logout", methods=["POST"])
def logout_page():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/register", methods=["GET", "POST"])
def register_page():
    """Self-service teacher signup.

    Creates a teacher account with its own isolated data partition
    ("teacher:<username>") on the default tier. Usernames are lowercase
    [a-z0-9_], 3-32 chars; passwords need 12-256 characters (same bar as the
    env-password roles). Anti-spam: per-IP and global daily signup caps, and
    cheap syntactic validation runs before any quota is consumed.
    """
    err = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        ip = _client_ip()
        # Validate first: malformed traffic must not burn the signup quota.
        if not re.fullmatch(r"[a-z0-9_]{3,32}", username):
            err = "Username must be 3-32 characters: lowercase letters, digits, underscore."
        elif username in _reserved_usernames():
            err = "That username is reserved. Please choose another."
        elif not 12 <= len(password) <= 256:
            err = "Password must be 12-256 characters."
        elif password != confirm:
            err = "Passwords do not match."
        elif db.get_user_by_username(username):
            # Taken: report without consuming quota (the UNIQUE constraint
            # remains the backstop for the check-then-act race).
            err = "That username is taken. Please choose another."
        elif not db.check_rate_limit(f"register:{ip}", "signup",
                                     REGISTER_MAX_PER_IP_PER_DAY):
            err = "Too many signups from this address today. Please try again tomorrow."
        elif not db.check_rate_limit("register", "signup",
                                     REGISTER_MAX_GLOBAL_PER_DAY):
            err = "Registration is temporarily paused. Please try again tomorrow."
        else:
            from werkzeug.security import generate_password_hash as _gen
            try:
                db.create_user(username, _gen(password), DEFAULT_TEACHER_TIER)
            except db.DuplicateUserError:
                err = "That username is taken. Please choose another."
            else:
                # Do not auto-login: a cross-site forged signup must not drop
                # the victim into an attacker-known account.
                return redirect(url_for("login_page"))
    return render_template("register.html", error=err)


def _save_upload(file_storage, prefix):
    ext = os.path.splitext(file_storage.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        ext = ".jpg"
    # Unpredictable, collision-free name. The old time+randint(1000,9999)
    # scheme had only 9,000 names per second per prefix, so a colliding
    # upload could flip a private image's owner row (and overwrite its bytes)
    # into the uploader's partition. uuid4 makes collisions and guessing
    # computationally infeasible.
    name = f"{prefix}_{uuid.uuid4().hex}{ext}"
    # Record the data partition for /uploads/<filename> authorization.
    # Insert-only: an existing row keeps its original owner (see
    # db.set_image_owner), so a collision can never transfer ownership.
    db.set_image_owner(name, _data_owner())
    try:
        if db.USE_POSTGRES:
            # Render disk is ephemeral: store image bytes in Postgres.
            data = file_storage.read()
            mime = file_storage.mimetype or "image/jpeg"
            db.save_image(name, data, mime)
            return f"db:{name}"
        path = os.path.join(UPLOAD_DIR, name)
        file_storage.save(path)
        return path
    except Exception:
        # The owner row was already inserted: roll it (and any partial file)
        # back so a failed upload never leaks rows or bytes.
        try:
            db.delete_image(name)
        except Exception:
            pass
        if not db.USE_POSTGRES:
            try:
                os.remove(os.path.join(UPLOAD_DIR, name))
            except OSError:
                pass
        raise


def _purge_upload(p):
    """Delete one stored upload's bytes and file, partition-safe.

    Only touches images owned by the caller's data partition: a planted
    cross-partition name can never destroy another partition's bytes.
    Filesystem deletes are contained to UPLOAD_DIR via realpath comparison
    (defense in depth — stored names are already normalized to basenames).
    """
    name = _upload_basename(p)
    if not name or db.get_image_owner(name) != _data_owner():
        return
    db.delete_image(name)
    if not db.USE_POSTGRES:
        upload_root = os.path.realpath(UPLOAD_DIR)
        path = os.path.realpath(os.path.join(UPLOAD_DIR, name))
        if os.path.dirname(path) == upload_root:
            try:
                os.remove(path)
            except OSError:
                pass


def _purge_uploads(paths):
    """Purge several stored uploads (see _purge_upload)."""
    for p in paths:
        _purge_upload(p)


def _stage_uploads(files, prefix):
    """Save uploads one by one; on ANY failure purge what was staged.

    Returns the list of stored paths. Re-raises the original exception after
    cleanup, so callers never leak staged bytes or owner rows when a
    multi-file batch fails partway through.
    """
    staged = []
    try:
        for f in files:
            staged.append(_save_upload(f, prefix))
        return staged
    except Exception:
        _purge_uploads(staged)
        raise


def _upload_exists(p):
    """Check if an upload path exists (handles db: prefix for Postgres mode)."""
    if p.startswith("db:"):
        data, _ = db.get_image(p[3:])
        return data is not None
    if p.startswith("db:"):
        data, _ = db.get_image(p[3:])
        return data is not None
    return os.path.exists(p)


def _upload_to_url(p):
    """Convert a stored upload path to a /uploads/ URL."""
    if p.startswith("db:"):
        return f"/uploads/{p[3:]}"
    return f"/uploads/{os.path.basename(p)}"


def _upload_basename(p):
    """Get the filename for a stored upload path (handles db: prefix)."""
    if p.startswith("db:"):
        return p[3:]
    return os.path.basename(p)


def _env_status():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    status = {
        "key_set": bool(key),
        "key_hint": ("…" + key[-4:]) if key and len(key) > 4 else "",
        "model": os.environ.get("OPENAI_MODEL", grader.DEFAULT_MODEL),
    }
    if _limited_limits() is not None:
        # Rate-limited roles (demo + registered teachers) must never see any
        # part of the owner's API key, not even the last-4 hint shown to the
        # owner's own roles.
        status["key_hint"] = ""
    return status


# ---------- Home: classes / assignments ----------
@app.route("/")
def index():
    classes = db.list_classes(_data_owner())
    for c in classes:
        c["assignments"] = db.list_assignments(c["id"])
    dup = request.args.get("dup")
    # Tell the template whether the public demo account is enabled, so the
    # login page can hint at it (the password itself is never exposed).
    return render_template("index.html", classes=classes, env=_env_status(),
                           dup_name=dup, is_public=_limited_limits() is not None)


@app.route("/class/new", methods=["POST"])
def class_new():
    name = (request.form.get("name") or "").strip()
    if name:
        _lim = _limited_limits()
        if _lim is not None:
            # Atomic cap: the count check and the insert run in one locked
            # transaction, so a parallel burst of requests cannot overshoot
            # the account's class limit (each class can hold images that
            # live in the owner's database).
            if db.create_class_capped(name, _data_owner(), _lim["max_classes"]) is None:
                return jsonify({"ok": False, "error":
                                f"{_limit_label()} limit reached: at most {_lim['max_classes']} classes "
                                f"for this account."}), 429
        else:
            db.create_class(name, _data_owner())
    return redirect(url_for("index"))


@app.route("/assignment/new", methods=["POST"])
def assignment_new():
    class_id = request.form.get("class_id")
    name = (request.form.get("name") or "").strip()
    if class_id and name:
        _require_class(int(class_id))  # 403 if cross-partition
        _lim = _limited_limits()
        if _lim is not None:
            # Atomic cap (see class_new).
            try:
                aid = db.create_assignment_capped(int(class_id), name, _data_owner(),
                                                  _lim["max_assignments"])
            except ValueError:
                # Duplicate name in this class — bounce back with an error flag.
                return redirect(url_for("index", dup=name))
            if aid is None:
                return jsonify({"ok": False, "error":
                                f"{_limit_label()} limit reached: at most {_lim['max_assignments']} assignments "
                                f"for this account."}), 429
        else:
            try:
                aid = db.create_assignment(int(class_id), name)
            except ValueError:
                # Duplicate name in this class — bounce back with an error flag.
                return redirect(url_for("index", dup=name))
        return redirect(url_for("assignment_page", assignment_id=aid))
    return redirect(url_for("index"))


# ---------- Assignment setup: rubric entry ----------
@app.route("/assignment/<int:assignment_id>")
def assignment_page(assignment_id):
    a = _require_assignment(assignment_id)
    items = json.loads(a["rubric_json"] or "[]")
    # Self-heal: normalize LaTeX delimiters in rubrics saved before the fix.
    for it in items:
        for k in ("question_text", "reference_solution", "criteria"):
            if it.get(k):
                it[k] = grader.normalize_latex(it[k])
    refs = json.loads(a["reference_images"] or "[]")
    refs = [_upload_to_url(p).split("/uploads/")[-1] for p in refs if _upload_exists(p)]
    rubric_examples = db.get_rubric_example_images(assignment_id)
    rubric_examples = [_upload_to_url(p).split("/uploads/")[-1] for p in rubric_examples if _upload_exists(p)]
    return render_template(
        "assignment.html", a=a, items=items, refs=refs,
        rubric_examples=rubric_examples, env=_env_status()
    )


# Background rubric-digitization jobs: digitizing + validating takes ~1 minute,
# too long for one fragile HTTP request. POST /api/rubric_from_example starts
# a job and returns immediately; the client polls /api/rubric_job/<job_id>.
@app.route("/api/rubric_job/<job_id>")
def api_rubric_job(job_id):
    # Jobs are DB-backed (not in-memory) so they survive gunicorn multi-worker
    # setups and server restarts. A "running" job with no update for a long
    # time means its worker died: report it as an error, not "not found".
    job = db.get_rubric_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found. The server may have restarted — please tap Generate again."}), 404
    # Data-partition authorization: jobs are tied to their assignment.
    # Pre-migration jobs have assignment_id=0 and are treated as private.
    if job.get("assignment_id"):
        _require_assignment(job["assignment_id"])  # 404/403 on cross-partition
    elif _limited_limits() is not None:
        return jsonify({"ok": False, "error": "Job not found."}), 404
    if job["status"] == "running" and time.time() - job["updated"] > db.JOB_STALE_SECONDS:
        db.fail_rubric_job(job_id, "The generation worker stopped unexpectedly — please tap Generate again.")
        job = db.get_rubric_job(job_id)
    # While running, "done" carries the questions validated so far (each with
    # "idx"); the client renders and saves them incrementally.
    out = {"ok": True, "status": job["status"], "total": job.get("total", 0),
           "done": job.get("done", [])}
    if job["status"] == "done":
        out["items"] = job["items"]
    elif job["status"] == "error":
        out["error"] = job.get("error", "Unknown error.")
    return jsonify(out)


@app.route("/api/solve_rubric", methods=["POST"])
def api_solve_rubric():
    """Start the unified rubric job; returns immediately.

    One button does both: the AI digitizes the photos (printed questions +
    point values; teacher's worked solutions/answer key IF shown). Per
    question: if a teacher reference solution was found it is validated and
    used (AP-style rubric auto-generated when the markings yield no usable
    rules); otherwise the AI solves it itself with the reasoning model
    (up to 3 attempts) and builds an AP-style rubric. Max scores always come
    from the printed point values. The client polls /api/rubric_job/<job_id>.
    """
    data = request.get_json(force=True, silent=True) or {}
    assignment_id = data.get("assignment_id")
    include = data.get("include") or []
    if not assignment_id:
        return jsonify({"ok": False, "error": "Missing assignment."}), 400
    a = _require_assignment(assignment_id)  # 404/403 on cross-partition access
    # Validate inputs BEFORE consuming any rate-limit quota: invalid requests
    # must not burn the shared demo budget.
    saved = db.get_rubric_example_images(assignment_id)
    saved_names = {_upload_basename(p): p for p in saved if _upload_exists(p)}
    paths = [saved_names[n] for n in include if n in saved_names]
    if not paths:
        return jsonify({"ok": False, "error": "No question photos selected. Please add them first."}), 400
    _lim = _limited_limits()
    if _lim is not None and len(paths) > _lim["max_rubric_images"]:
        return jsonify({"ok": False, "error":
                        f"{_limit_label()} limit: at most {_lim['max_rubric_images']} photos per rubric generation."}), 400
    # Public demo guardrails: lifetime cap + token budget first (these don't
    # consume quota), then the daily operation cap immediately before
    # starting costly work.
    err = _limited_lifetime_cap_error()
    if err:
        return jsonify({"ok": False, "error": err}), 429
    if _limited_token_budget_exceeded():
        return jsonify({"ok": False,
                        "error": f"{_limit_label()} token budget exhausted for today. Please try again tomorrow."}), 429
    err = _limited_rate_limit_exceeded("rubric_job")
    if err:
        return jsonify({"ok": False, "error": err}), 429
    job_id = uuid.uuid4().hex[:12]
    db.cleanup_old_rubric_jobs()
    db.create_rubric_job(job_id, assignment_id=int(assignment_id))
    # Capture the caller's role for the background thread: each thread has its
    # own ContextVar state, so pass it explicitly.
    role = _data_owner()
    threading.Thread(target=_run_solve_job, args=(job_id, paths, role), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


def _run_solve_job(job_id, paths, role="private"):
    grader._set_current_operation("solve_rubric")
    grader._set_current_role(role)
    # Job state lives in the DB (not process memory) so any gunicorn worker
    # can serve poll requests and jobs survive restarts.
    def _on_total(total):
        db.set_rubric_job_total(job_id, total)

    def _on_item(item):
        db.append_rubric_job_item(job_id, item)

    try:
        result = grader.build_unified_rubric_streaming(
            paths, upload_dir=UPLOAD_DIR, on_total=_on_total, on_item=_on_item)
        db.finish_rubric_job(job_id, result["items"])
        # Public demo lifetime cap: count each generated question.
        if role == "public":
            db.add_lifetime_usage("public", "questions", len(result["items"]))
    except RuntimeError as e:
        db.fail_rubric_job(job_id, str(e))
    except Exception as e:  # noqa: BLE001
        db.fail_rubric_job(job_id, f"Failed to solve and generate rubric: {e}")




@app.route("/api/retry_question", methods=["POST"])
def api_retry_question():
    """Retry AI-solving a single question with debug output.

    Body: {assignment_id, q, question_text, max_score, model, high_effort}.
    `model` is "o3" (default) or "gpt-4o"; `high_effort` requests o3 with
    reasoning_effort="high". The public demo role is always forced onto the
    allowlisted public model (see grader.PUBLIC_MODEL). Returns the new solution, criteria, verification
    status, and the raw model outputs for debugging.
    """
    grader._set_current_operation("retry_question")
    data = request.get_json(force=True, silent=True) or {}
    assignment_id = data.get("assignment_id")
    q_label = str(data.get("q", "")).strip()
    question_text = str(data.get("question_text", "")).strip()
    model = str(data.get("model", "o3")).strip() or "o3"
    high_effort = bool(data.get("high_effort"))
    try:
        max_score = float(data.get("max_score", 0) or 0)
    except (TypeError, ValueError):
        max_score = 0
    if not assignment_id or not question_text:
        return jsonify({"ok": False, "error": "Missing assignment or question text."}), 400
    if model not in ("o3", "gpt-4o"):
        return jsonify({"ok": False, "error": "Model must be 'o3' or 'gpt-4o'."}), 400
    _lim = _limited_limits()
    if _lim is not None and _limited_text_too_long(q_label, question_text):
        return jsonify({"ok": False, "error":
                        f"{_limit_label()} limit: question label or question text too long "
                        f"(max {_lim['max_text_chars']} characters each)."}), 400
    if _is_public():
        # The demo may not pick the reasoning model: keep per-call cost bounded.
        # Use the allowlisted public model (not a "gpt-4o" literal) so the
        # guarantee holds even if the default model env is a reasoning model.
        model = grader.PUBLIC_MODEL
        high_effort = False
    _require_assignment(assignment_id)  # 404/403 on cross-partition access
    # Public demo guardrails: lifetime cap + token budget first (no quota
    # consumed), then the daily retry cap immediately before costly work.
    err = _limited_lifetime_cap_error()
    if err:
        return jsonify({"ok": False, "error": err}), 429
    if _limited_token_budget_exceeded():
        return jsonify({"ok": False,
                        "error": f"{_limit_label()} token budget exhausted for today. Please try again tomorrow."}), 429
    err = _limited_rate_limit_exceeded("retry")
    if err:
        return jsonify({"ok": False, "error": err}), 429
    # Give the solver the assignment's question photos for a second read.
    # Demo accounts get at most 1 photo of context (one photo per question).
    image_paths = [p for p in db.get_rubric_example_images(assignment_id) if _upload_exists(p)]
    _lim = _limited_limits()
    if _is_public():
        image_paths = image_paths[:1]
    elif _lim is not None:
        image_paths = image_paths[:_lim["max_rubric_images"]]
    try:
        result = grader.retry_solve_question(
            question_text, max_score, q_label,
            model=model, high_effort=high_effort,
            image_paths=image_paths or None,
        )
    except grader.RetryExhaustedError as e:
        # Expected failure mode (AI could not solve after retries) — return
        # ok:false with HTTP 200 so the frontend shows the message instead of
        # "Server returned 500". (Bug 2026-10-09, E2E v3 Q2/Q4.)
        return jsonify({"ok": False, "error": str(e)}), 200
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Retry failed: {e}"}), 500
    # Public demo lifetime cap: each retry fixes one question.
    if _limited_limits() is not None:
        db.add_lifetime_usage(_data_owner(), "questions", 1)
    return jsonify({"ok": True, "result": result})

@app.route("/api/regenerate_rubric", methods=["POST"])
def api_regenerate_rubric():
    """Regenerate AP-style rubric from the current reference solution.

    Body: {assignment_id, q, question_text, reference_solution, max_score}.
    Uses generate_ap_rubric_only with the AP enforcement (5 attempts + fallback).
    Returns {ok, criteria}.
    """
    grader._set_current_operation("regenerate_rubric")
    data = request.get_json(force=True, silent=True) or {}
    q_label = str(data.get("q", "")).strip()
    question_text = str(data.get("question_text", "")).strip()
    reference_solution = str(data.get("reference_solution", "")).strip()
    try:
        max_score = float(data.get("max_score", 0) or 0)
    except (TypeError, ValueError):
        max_score = 0
    if not question_text:
        return jsonify({"ok": False, "error": "Question text is empty."}), 400
    if not reference_solution:
        return jsonify({"ok": False, "error": "Reference solution is empty."}), 400
    if max_score <= 0:
        return jsonify({"ok": False, "error": "Max score must be positive."}), 400
    _lim = _limited_limits()
    if _lim is not None and _limited_text_too_long(q_label, question_text, reference_solution):
        return jsonify({"ok": False, "error":
                        f"{_limit_label()} limit: question label, question text, or reference solution "
                        f"too long (max {_lim['max_text_chars']} characters each)."}), 400
    try:
        assignment_id = int(data["assignment_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid assignment ID."}), 400
    _require_assignment(assignment_id)  # 404/403 on cross-partition access
    # Public demo guardrails: lifetime cap + token budget first (no quota
    # consumed), then the daily retry cap immediately before costly work.
    err = _limited_lifetime_cap_error()
    if err:
        return jsonify({"ok": False, "error": err}), 429
    if _limited_token_budget_exceeded():
        return jsonify({"ok": False,
                        "error": f"{_limit_label()} token budget exhausted for today. Please try again tomorrow."}), 429
    err = _limited_rate_limit_exceeded("retry")
    if err:
        return jsonify({"ok": False, "error": err}), 429
    try:
        criteria = grader.generate_ap_rubric_only(
            question_text, reference_solution, max_score, q_label)
    except Exception as e:  # noqa: BLE001
        # generate_ap_rubric_only raises on validation failure; fall back
        # to structural AP format so the teacher always gets usable output.
        criteria = grader._minimal_ap_rubric(reference_solution, max_score, q_label)
    # Rate-limited lifetime cap: each regeneration fixes one question.
    if _limited_limits() is not None:
        db.add_lifetime_usage(_data_owner(), "questions", 1)
    # Generate diagram if the question needs one (user requirement 2026-10-08:
    # "有图的你要画图" - questions mentioning graph/sketch must have diagrams).
    graph = None
    graph_error = ""
    try:
        q_for_graph = {"question_text": question_text,
                       "reference_solution": reference_solution,
                       "has_diagram": False, "diagram_hint": ""}
        graph = grader._process_graph(q_for_graph, [], UPLOAD_DIR)
        if graph is None:
            graph_error = "process_graph returned None (no diagram needed or generation failed)"
    except Exception as e:
        graph_error = f"{type(e).__name__}: {e}"
        import traceback
        traceback.print_exc()
    return jsonify({"ok": True, "criteria": criteria, "graph": graph, "graph_error": graph_error})


@app.route("/assignment/<int:assignment_id>/upload_rubric_example", methods=["POST"])
def assignment_upload_rubric_example(assignment_id):
    a = _require_assignment(assignment_id)
    new_files = [f for f in request.files.getlist("images") if f and f.filename]
    # Stage-then-atomic-append (see assignment_upload_reference): a parallel
    # upload burst cannot overshoot the cap, and no failure path leaks bytes.
    staged = _stage_uploads(new_files, "rubric_example")
    cap = (_limited_limits() or {}).get("max_example_images")
    try:
        appended = db.try_append_image_paths(
            assignment_id, "rubric_example_images", staged, cap)
    except Exception:
        _purge_uploads(staged)
        raise
    if not appended:
        _purge_uploads(staged)
        _lim = _limited_limits()
        if _lim is not None:
            return jsonify({"ok": False, "error":
                            f"{_limit_label()} limit: at most {_lim['max_example_images']} example photos "
                            f"per assignment."}), 400
        return jsonify({"ok": False, "error": "Assignment not found."}), 404
    names = [_upload_basename(p) for p in db.get_rubric_example_images(assignment_id)
             if _upload_exists(p)]
    return jsonify({"ok": True, "examples": names})


@app.route("/assignment/<int:assignment_id>/delete_rubric_example", methods=["POST"])
def assignment_delete_rubric_example(assignment_id):
    _require_assignment(assignment_id)  # 404/403 on cross-partition access
    data = request.get_json(force=True, silent=True) or {}
    name = _upload_basename(data.get("filename") or "")
    # Atomic remove under the assignment-images lock: a concurrent upload
    # cannot be silently lost, and the removed photos' bytes are purged in
    # both storage modes (otherwise a remove/re-upload loop grows storage
    # without bound despite the per-assignment caps).
    removed = db.try_remove_image_path(assignment_id, "rubric_example_images", name)
    for p in removed:
        _purge_upload(p)
    names = [_upload_basename(p) for p in db.get_rubric_example_images(assignment_id)
             if _upload_exists(p)]
    return jsonify({"ok": True, "examples": names})


@app.route("/assignment/<int:assignment_id>/save_rubric", methods=["POST"])
def assignment_save_rubric(assignment_id):
    _require_assignment(assignment_id)  # 404/403 on cross-partition access
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    # The form's items win (the teacher may have edited them after AI generation).
    clean = []
    for it in items:
        q = str(it.get("q", "")).strip()
        if not q:
            continue
        try:
            ms = float(it.get("max_score", 0))
        except (TypeError, ValueError):
            ms = 0
        clean.append({
            "q": q,
            "max_score": ms,
            "criteria": str(it.get("criteria", "")).strip(),
            "question_text": str(it.get("question_text", "")).strip(),
            "reference_solution": str(it.get("reference_solution", "")).strip(),
            "verified": it.get("verified", "review") if it.get("verified") in ("yes", "review") else "review",
            "verify_note": str(it.get("verify_note", "")).strip(),
            "graph": it.get("graph") if isinstance(it.get("graph"), dict) else None,
        })
    items = clean
    rubric_text = grader.rubric_items_to_text(items)
    if not rubric_text.strip():
        return jsonify({"ok": False, "error": "The rubric is empty. Please generate it from a reference example in Step 1 first."}), 400
    # Stored rubrics are sent to the model on every grading: cap the stored
    # size for the demo account so a huge saved rubric can't bypass the
    # per-request text caps.
    _lim = _limited_limits()
    if _lim is not None and len(rubric_text) + len(json.dumps(items)) > _lim["max_rubric_chars"]:
        return jsonify({"ok": False, "error":
                        f"{_limit_label()} limit: rubric too long (max {_lim['max_rubric_chars']} characters)."}), 400
    db.save_rubric(assignment_id, rubric_text, items)
    return jsonify({"ok": True, "rubric_text": rubric_text, "items": items})


@app.route("/assignment/<int:assignment_id>/upload_reference", methods=["POST"])
def assignment_upload_reference(assignment_id):
    files = request.files.getlist("images")
    a = _require_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    new_files = [f for f in files if f and f.filename]
    # Stage the uploads first (any mid-batch failure purges what was staged),
    # then append atomically: the count check and the list update run in one
    # locked transaction, so a parallel upload burst cannot overshoot the
    # cap. Rejected or failed appends purge the staged uploads.
    staged = _stage_uploads(new_files, "ref")
    cap = (_limited_limits() or {}).get("max_reference_images")
    try:
        appended = db.try_append_image_paths(
            assignment_id, "reference_images", staged, cap)
    except Exception:
        _purge_uploads(staged)
        raise
    if not appended:
        _purge_uploads(staged)
        _lim = _limited_limits()
        if _lim is not None:
            return jsonify({"ok": False, "error":
                            f"{_limit_label()} limit: at most {_lim['max_reference_images']} reference photos "
                            f"per assignment."}), 400
        return jsonify({"ok": False, "error": "Assignment not found."}), 404
    return redirect(url_for("assignment_page", assignment_id=assignment_id))


# ---------- Grading ----------
@app.route("/api/assignment/<int:assignment_id>/delete", methods=["POST"])
def api_assignment_delete(assignment_id):
    _require_assignment(assignment_id)  # 404/403 before deleting
    # Gather names and delete rows atomically (same lock as uploads): a
    # concurrent upload cannot slip bytes in after the snapshot and orphan
    # them. Purge image bytes afterwards: in Postgres mode they live in the
    # owner's database, so deleting the rows without the bytes would leak
    # storage. Each name is ownership-checked: a planted cross-partition name
    # can never destroy another partition's image, and a shared image (legacy
    # name collision) is kept while another assignment still references it.
    for name in db.delete_assignment_returning_images(assignment_id):
        if db.get_image_owner(name) != _data_owner():
            continue
        if db.image_referenced_elsewhere(name, exclude_assignment_id=assignment_id):
            continue
        _purge_upload("db:" + name)
    return jsonify({"ok": True})


@app.route("/grade/<int:assignment_id>")
def grade_page(assignment_id):
    a = _require_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    return render_template("grade.html", a=a, env=_env_status())


@app.route("/api/extract_name", methods=["POST"])
def api_extract_name():
    """Extract student name from an uploaded paper photo.

    Body: multipart with 'image' file. Returns {ok, name}.
    """
    grader._set_current_operation("extract_name")
    # Blocked for the demo account: this is an OpenAI vision call outside the
    # grading rate limits and token budget. Public grading (/api/grade)
    # already auto-extracts the name inside its guarded operation.
    if _limited_limits() is not None:
        return jsonify({"ok": False, "error":
                        "Standalone name extraction is unavailable for limited accounts. "
                        "Grade a paper instead — the name is extracted automatically."}), 403
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No image."}), 400
    # Save temporarily, extract, then clean up
    import tempfile
    ext = ".jpg"
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            f.save(tmp.name)
            tmp_path = tmp.name
        name = grader.extract_student_name(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    return jsonify({"ok": True, "name": name})


@app.route("/api/grade", methods=["POST"])
def api_grade():
    grader._set_current_operation("grade")
    raw_assignment_id = request.form.get("assignment_id")
    student_name = (request.form.get("student_name") or "").strip()
    try:
        assignment_id = int(raw_assignment_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid assignment ID."}), 400
    a = _require_assignment(assignment_id)
    # Validate inputs BEFORE consuming rate-limit quota.
    files = request.files.getlist("images")
    valid_files = [f for f in files if f and f.filename]
    _lim = _limited_limits()
    if _lim is not None and len(valid_files) > _lim["max_grade_images"]:
        return jsonify({"ok": False, "error":
                        f"{_limit_label()} limit: at most {_lim['max_grade_images']} photos per paper."}), 400
    if not valid_files:
        return jsonify({"ok": False, "error": "No paper photos received. Please take/upload photos first."}), 400
    # Reference (answer-key) photos are also sent to the model, so for the
    # demo account the *total* images per grading (paper + reference) must
    # stay within the photo cap — otherwise stored reference images would
    # bypass it.
    ref_paths = [p for p in json.loads(a["reference_images"] or "[]") if _upload_exists(p)]
    if _lim is not None and len(valid_files) + len(ref_paths) > _lim["max_grade_images"]:
        return jsonify({"ok": False, "error":
                        f"{_limit_label()} limit: at most {_lim['max_grade_images']} total photos per grading "
                        f"(paper + reference). Remove some reference photos first."}), 400
    # The stored rubric is sent to the model on every grading: reject an
    # over-long saved rubric for the demo account (defense in depth with the
    # write-time cap in assignment_save_rubric).
    _lim = _limited_limits()
    if _lim is not None and _limited_rubric_too_long(a):
        return jsonify({"ok": False, "error":
                        f"{_limit_label()} limit: rubric too long (max {_lim['max_rubric_chars']} characters). "
                        f"Shorten the rubric first."}), 400
    # Public demo guardrails: lifetime cap + token budget first (no quota
    # consumed), then the daily grading cap immediately before costly work.
    if _limited_token_budget_exceeded():
        return jsonify({"ok": False,
                        "error": f"{_limit_label()} token budget exhausted for today. Please try again tomorrow."}), 429
    err = _limited_rate_limit_exceeded("grade")
    if err:
        return jsonify({"ok": False, "error": err}), 429

    # Any failure from here on (a mid-batch upload error, name extraction, or
    # the grading call itself) discards the staged paper photos: no orphaned
    # bytes. _stage_uploads already purges on mid-batch save failures.
    paths = []
    try:
        first_path = None
        for f in valid_files:
            p = _save_upload(f, "paper")
            paths.append(p)
            if first_path is None:
                first_path = p
        # Auto-extract student name from first photo if not provided
        if not student_name and first_path:
            try:
                # Resolve db: path to a real file for the extractor
                extract_path = first_path
                if first_path.startswith("db:"):
                    import tempfile
                    data, _ = db.get_image(first_path[3:])
                    if data:
                        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                            tmp.write(data)
                            extract_path = tmp.name
                student_name = grader.extract_student_name(extract_path) or ""
                if first_path.startswith("db:") and extract_path != first_path:
                    try:
                        os.unlink(extract_path)
                    except Exception:
                        pass
            except Exception:
                pass
        if not student_name:
            student_name = "Student"

        items = json.loads(a["rubric_json"] or "[]")
        result = grader.grade_paper(a["rubric_text"], items, paths, ref_paths)
    except RuntimeError as e:
        _purge_uploads(paths)
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        _purge_uploads(paths)
        return jsonify({"ok": False, "error": f"Grading failed: {e}"}), 500
    return jsonify({"ok": True, "result": result, "photos": [_upload_basename(p) for p in paths]})


@app.route("/api/save_grade", methods=["POST"])
def api_save_grade():
    data = request.get_json(force=True, silent=True) or {}
    try:
        assignment_id = int(data["assignment_id"])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"Save failed, incomplete data: {e}"}), 400
    _require_assignment(assignment_id)  # 404/403 on cross-partition access
    try:
        gid = db.save_grade(
            assignment_id,
            (data.get("student_name") or "").strip(),
            data.get("questions") or [],
            float(data.get("total", 0)),
            float(data.get("max_total", 0)),
            data.get("overall_feedback") or "",
            data.get("photos") or [],
        )
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"Save failed, incomplete data: {e}"}), 400
    return jsonify({"ok": True, "id": gid})


# ---------- Results & stats ----------
@app.route("/results/<int:assignment_id>")
def results_page(assignment_id):
    a = _require_assignment(assignment_id)
    grades = db.list_grades(assignment_id)
    totals = [g["total"] for g in grades]
    stats = {}
    if totals:
        stats = {
            "count": len(totals),
            "avg": round(sum(totals) / len(totals), 2),
            "max": max(totals),
            "min": min(totals),
        }
    return render_template("results.html", a=a, grades=grades, stats=stats, env=_env_status())


@app.route("/results/<int:assignment_id>/csv")
def results_csv(assignment_id):
    a = _require_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    grades = db.list_grades(assignment_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Total", "Max", "Overall feedback", "Graded at"])
    for g in grades:
        w.writerow([g["student_name"], g["total"], g["max_total"], g["overall_feedback"], g["created_at"]])
    mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    filename = f"grades_{a['name']}_{datetime.now().strftime('%Y%m%d')}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)


@app.route("/graded/<int:grade_id>")
def grade_detail_page(grade_id):
    g = _require_grade(grade_id)
    if not g:
        return redirect(url_for("index"))
    a = _require_assignment(g["assignment_id"])
    try:
        questions = json.loads(g["questions_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        questions = []
    try:
        photos = json.loads(g.get("images") or "[]")
    except (json.JSONDecodeError, TypeError):
        photos = []
    # Only keep photos that still exist on disk.
    photos = [p for p in photos if _upload_exists(p if p.startswith("db:") else os.path.join(UPLOAD_DIR, p))]
    return render_template("grade_detail.html", a=a, g=g, questions=questions, photos=photos, env=_env_status())


# ---------- Settings ----------
@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    # Require login (tester or admin). Rate-limited roles (demo + registered
    # teachers) are blocked here: they must never see or change the owner's
    # OpenAI key.
    _require_login()
    if _limited_limits() is not None:
        abort(403)
    msg = ""
    if request.method == "POST":
        api_key = (request.form.get("api_key") or "").strip()
        model = (request.form.get("model") or "").strip() or grader.DEFAULT_MODEL
        _write_env(api_key, model)
        # Apply to the current process as well.
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_MODEL"] = model
        msg = "Saved."
    usage = db.get_api_usage_summary(since_ts=time.time() - 30 * 86400)
    return render_template("settings.html", env=_env_status(), msg=msg, usage=usage)


# ---------- Public demo account monitoring (admin only) ----------
@app.route("/admin/public")
def public_monitor_page():
    """Admin dashboard for the public demo account: usage, rate limits, costs.

    Shows what the public account used (ops, tokens, $ estimate), current
    rate-limit counters, and recent API calls — so the owner can monitor and
    manage demo usage.
    """
    if not _is_admin():
        abort(403)
    day = db._rate_limit_day()
    usage = db.get_api_usage_summary_for_role("public", day)
    token_budget = PUBLIC_DAILY_TOKEN_BUDGET
    tokens_used = usage["prompt_tokens"] + usage["completion_tokens"]
    lifetime_questions = db.get_lifetime_usage("public", "questions")
    limits = {
        "rubric_job": {
            "label": "Rubric generations",
            "limit": PUBLIC_MAX_RUBRIC_JOBS_PER_DAY,
            "used": db.get_rate_limit_usage("public", "rubric_job", day),
        },
        "grade": {
            "label": "Paper gradings",
            "limit": PUBLIC_MAX_GRADES_PER_DAY,
            "used": db.get_rate_limit_usage("public", "grade", day),
        },
        "retry": {
            "label": "Question retries",
            "limit": PUBLIC_MAX_RETRIES_PER_DAY,
            "used": db.get_rate_limit_usage("public", "retry", day),
        },
    }
    recent = db.get_recent_api_usage(role="public", limit=30)
    public_enabled = bool(PUBLIC_PASSWORD)
    return render_template(
        "public_admin.html",
        day=day,
        usage=usage,
        tokens_used=tokens_used,
        token_budget=token_budget,
        lifetime_questions=lifetime_questions,
        lifetime_cap=PUBLIC_MAX_QUESTIONS_LIFETIME,
        limits=limits,
        recent=recent,
        public_enabled=public_enabled,
        public_user=PUBLIC_USER,
        env=_env_status(),
    )


@app.route("/admin/public/reset_limits", methods=["POST"])
def public_reset_limits():
    """Clear today's rate-limit counters for the public role (admin only)."""
    if not _is_admin():
        abort(403)
    db.reset_rate_limits("public")
    return redirect(url_for("public_monitor_page"))


# ---------- Registered teachers (admin only) ----------

_LIMIT_LABELS = {
    "max_rubric_jobs_per_day": "Rubric generations / day",
    "max_grades_per_day": "Paper gradings / day",
    "max_retries_per_day": "Question retries / day",
    "daily_token_budget": "Token budget / day",
    "max_questions_lifetime": "Questions lifetime (0 = unlimited)",
    "max_text_chars": "Text input chars",
    "max_rubric_chars": "Rubric chars",
    "max_reference_images": "Reference images / assignment",
    "max_example_images": "Example images / assignment",
    "max_rubric_images": "Rubric-gen images",
    "max_grade_images": "Grade images / paper",
    "max_classes": "Classes",
    "max_assignments": "Assignments",
}


def _teacher_effective_limits(user):
    """Merge tier defaults with the teacher's overrides (for display)."""
    tier = TEACHER_TIERS.get(user.get("tier") or "", TEACHER_TIERS[DEFAULT_TEACHER_TIER])
    merged = dict(tier)
    try:
        overrides = json.loads(user.get("limits_json") or "{}")
    except Exception:
        overrides = {}
    clean = {}
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k in _LIMIT_KEYS:
                try:
                    clean[k] = int(v)
                except (TypeError, ValueError):
                    pass
    merged.update(clean)
    return merged, clean


@app.route("/admin/teachers")
def teachers_page():
    """Admin dashboard: registered teachers, tiers, usage, limits."""
    if not _is_admin():
        abort(403)
    day = db._rate_limit_day()
    teachers = []
    for u in db.list_users():
        owner = f"teacher:{u['id']}"
        usage = db.get_api_usage_summary_for_role(owner, day)
        tokens = usage["prompt_tokens"] + usage["completion_tokens"]
        effective, _ = _teacher_effective_limits(u)
        teachers.append({
            "id": u["id"],
            "username": u["username"],
            "tier": u.get("tier") or DEFAULT_TEACHER_TIER,
            "disabled": bool(u.get("disabled")),
            "created_at": u.get("created_at"),
            "tokens_today": tokens,
            "calls_today": usage.get("calls", 0),
            "daily_token_budget": effective["daily_token_budget"],
            "lifetime_questions": db.get_lifetime_usage(owner, "questions"),
        })
    return render_template(
        "teachers.html",
        teachers=teachers,
        tiers=sorted(TEACHER_TIERS),
        env=_env_status(),
    )


@app.route("/admin/teachers/<username>")
def teacher_detail_page(username):
    """Admin detail: a teacher's effective limits with per-key overrides."""
    if not _is_admin():
        abort(403)
    user = db.get_user_by_username(username)
    if not user:
        abort(404)
    effective, overrides = _teacher_effective_limits(user)
    tier_defaults = TEACHER_TIERS.get(user.get("tier") or "",
                                      TEACHER_TIERS[DEFAULT_TEACHER_TIER])
    rows = [{"key": k, "label": _LIMIT_LABELS[k],
             "tier_default": tier_defaults[k],
             "override": overrides.get(k, ""),
             "effective": effective[k]} for k in _LIMIT_KEYS]
    owner = f"teacher:{user['id']}"
    day = db._rate_limit_day()
    usage = db.get_api_usage_summary_for_role(owner, day)
    return render_template(
        "teacher_detail.html",
        user=user,
        tiers=sorted(TEACHER_TIERS),
        rows=rows,
        tokens_today=usage["prompt_tokens"] + usage["completion_tokens"],
        lifetime_questions=db.get_lifetime_usage(owner, "questions"),
        env=_env_status(),
    )


@app.route("/admin/teachers/<username>/tier", methods=["POST"])
def teacher_set_tier(username):
    """Assign a tier (resets per-teacher overrides). Admin only."""
    if not _is_admin():
        abort(403)
    user = db.get_user_by_username(username)
    if not user:
        abort(404)
    tier = (request.form.get("tier") or "").strip()
    if tier not in TEACHER_TIERS:
        abort(400)
    db.set_user_tier(user["id"], tier)
    return redirect(url_for("teacher_detail_page", username=username))


@app.route("/admin/teachers/<username>/limits", methods=["POST"])
def teacher_set_limits(username):
    """Save per-teacher limit overrides (empty input = tier default)."""
    if not _is_admin():
        abort(403)
    user = db.get_user_by_username(username)
    if not user:
        abort(404)
    overrides = {}
    for k in _LIMIT_KEYS:
        raw = (request.form.get(k) or "").strip()
        if raw == "":
            continue
        try:
            v = int(raw)
        except ValueError:
            abort(400)
        if v < 0:
            abort(400)
        overrides[k] = v
    db.set_user_limits(user["id"], overrides)
    return redirect(url_for("teacher_detail_page", username=username))


@app.route("/admin/teachers/<username>/disable", methods=["POST"])
def teacher_set_disabled(username):
    """Disable or re-enable a teacher account (takes effect immediately)."""
    if not _is_admin():
        abort(403)
    user = db.get_user_by_username(username)
    if not user:
        abort(404)
    db.set_user_disabled(user["id"], request.form.get("disabled") == "1")
    return redirect(url_for("teachers_page"))


@app.template_filter("timestamp_to_time")
def _timestamp_to_time_filter(ts):
    """Format an epoch timestamp as HH:MM:SS server-local for admin tables."""
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


@app.context_processor
def _inject_role():
    """Make the current role available to all templates (for nav links)."""
    return {"is_admin": _is_admin(), "is_limited_role": _limited_limits() is not None}


def _write_env(api_key, model):
    """Write the key into the project-root .env (server-local only, never committed to git)."""
    env_path = os.path.join(BASE_DIR, ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    out, seen_key, seen_model = [], False, False
    for ln in lines:
        if ln.startswith("OPENAI_API_KEY="):
            out.append(f"OPENAI_API_KEY={api_key}" if api_key else ln)
            seen_key = True
        elif ln.startswith("OPENAI_MODEL="):
            out.append(f"OPENAI_MODEL={model}")
            seen_model = True
        else:
            out.append(ln)
    if api_key and not seen_key:
        out.append(f"OPENAI_API_KEY={api_key}")
    if not seen_model:
        out.append(f"OPENAI_MODEL={model}")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).strip() + "\n")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    # Block path traversal (e.g. ../../ai_grader.db). Also reject backslashes
    # (Windows-style traversal) for defense in depth.
    if "\\" in filename or "/" in filename or filename.startswith("."):
        return jsonify({"ok": False, "error": "Not found."}), 404
    # Data-partition authorization: a public demo user must not fetch private
    # images by guessing filenames. Unknown (pre-migration) images are treated
    # as private, so only the owner's roles can fetch them.
    owner = db.get_image_owner(filename)
    if owner is None:
        owner = "private"
    if owner != _data_owner():
        return jsonify({"ok": False, "error": "Not found."}), 404
    # Postgres mode: images live in the DB (Render disk is ephemeral).
    if db.USE_POSTGRES:
        data, mime = db.get_image(filename)
        if data is None:
            return jsonify({"ok": False, "error": "Not found."}), 404
        return send_file(io.BytesIO(data), mimetype=mime or "image/jpeg")
    safe = os.path.realpath(os.path.join(UPLOAD_DIR, filename))
    if not safe.startswith(os.path.realpath(UPLOAD_DIR) + os.sep):
        return jsonify({"ok": False, "error": "Not found."}), 404
    if not os.path.isfile(safe):
        return jsonify({"ok": False, "error": "Not found."}), 404
    return send_file(safe)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

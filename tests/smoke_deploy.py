#!/usr/bin/env python3
"""Post-deployment smoke test for the AI Grader.

Verifies the live deployment end-to-end after every deploy:
login gate, roles, API auth, key pages, and OpenAI key status.

Usage:
    cd ~/workspace/ai-grader
    DEPLOY_URL=https://ai-grader-shwa.onrender.com \
    GRADER_PASSWORD='<tester-pw>' ADMIN_PASSWORD='<admin-pw>' \
    .venv/bin/python tests/smoke_deploy.py

All credentials come from env vars (never hardcoded). Exits 0 if all
checks pass, 1 otherwise.
"""
import os
import sys
import urllib.request
import urllib.parse
import http.cookiejar

BASE = os.environ.get("DEPLOY_URL", "").rstrip("/")
TESTER_PW = os.environ.get("GRADER_PASSWORD", "")
ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "")
USER = os.environ.get("GRADER_USER", "teacher")

errors = []


def check(name, cond, detail=""):
    status = "✅" if cond else "❌"
    print(f"  {status} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        errors.append(name)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Session:
    """Minimal cookie-aware HTTP client (no extra deps, no redirect following)."""

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj), _NoRedirect)

    def get(self, path, headers=None):
        req = urllib.request.Request(BASE + path, headers=headers or {})
        try:
            r = self.opener.open(req, timeout=30)
            return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers

    def post(self, path, data):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(BASE + path, data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            r = self.opener.open(req, timeout=30)
            return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers


def main():
    if not BASE or not TESTER_PW or not ADMIN_PW:
        print("Set DEPLOY_URL, GRADER_PASSWORD, ADMIN_PASSWORD env vars.")
        return 1

    print(f"Smoke-testing {BASE} ...")

    # 1. Login page loads (unauthenticated).
    s = Session()
    code, body, _ = s.get("/login")
    check("login page loads", code == 200, f"got {code}")

    # 2. Home redirects to login when unauthenticated.
    code, _, headers = s.get("/")
    check("unauth home -> login", code in (301, 302, 303),
          f"got {code}")

    # 3. Unauthenticated API returns JSON 401 (not HTML).
    code, body, _ = s.get("/api/rubric_job/xyz")
    check("unauth API -> 401 JSON", code == 401 and "auth_required" in body,
          f"got {code}")

    # 4. Wrong password rejected.
    code, body, _ = s.post("/login", {"username": USER, "password": "wrong"})
    check("wrong password rejected", code == 200 and "Wrong" in body,
          f"got {code}")

    # 5. Empty password rejected (fail-closed).
    code, body, _ = s.post("/login", {"username": USER, "password": ""})
    check("empty password rejected", code == 200 and "Wrong" in body,
          f"got {code}")

    # 6. Tester login works; home loads; settings blocked.
    st = Session()
    code, _, _ = st.post("/login", {"username": USER, "password": TESTER_PW})
    check("tester login", code in (301, 302, 303), f"got {code}")
    code, body, _ = st.get("/")
    check("tester home loads", code == 200 and "AI Grader" in body,
          f"got {code}")
    code, _, _ = st.get("/settings")
    check("tester blocked from /settings", code in (301, 302, 303),
          f"got {code}")

    # 7. Admin login works; settings loads with usage section.
    sa = Session()
    code, _, _ = sa.post("/login", {"username": USER, "password": ADMIN_PW})
    check("admin login", code in (301, 302, 303), f"got {code}")
    code, body, _ = sa.get("/settings")
    check("admin settings loads", code == 200 and "API Usage" in body,
          f"got {code}")

    # 8. OpenAI key is configured (no warning banner).
    check("OpenAI key configured", "No OpenAI API key configured" not in body)

    # 9. Upgrade-to-AP button is gone (AP is default).
    code, body, _ = sa.get("/assignment/new")
    # /assignment/new may not exist; check home instead for absence.
    code, body, _ = sa.get("/")
    check("no Upgrade-to-AP remnants", "upgrade_to_ap" not in body.lower())

    print()
    if errors:
        print(f"FAILED: {len(errors)} check(s)")
        return 1
    print("All smoke checks passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())

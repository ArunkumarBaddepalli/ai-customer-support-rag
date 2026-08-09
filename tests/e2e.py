"""End-to-end test across every user perspective.

Covers the visitor, a new business owner signing up and onboarding, their
customers chatting, a second business (isolation), and an attacker.

Each persona gets its own cookie jar, so sessions behave exactly as separate
browsers would. Requests are sent with urllib rather than curl on purpose:
shell quoting silently mangled test values more than once and produced
failures that looked like application bugs.

    python app.py          # in one terminal
    python tests/e2e.py    # in another

Exits non-zero if any check fails.
"""

import http.cookiejar
import json
from datetime import datetime, timedelta, timezone
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# db.py lives one directory up — connect to the same database the running
# app is using, so these checks can inspect tokens the app issued.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = "http://127.0.0.1:5001"

results = []


# Read .env the same way app.py does. Without this the key sits in .env, the
# app happily uses it, and the suite silently decides there is no LLM and skips
# a third of its coverage — passing while testing less.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             ".env"))
except ImportError:
    pass

# Answer-quality checks need a real LLM. Without a key the bot still responds —
# with the "can't answer right now" fallback — so those checks would fail on a
# working app. They are skipped instead, and counted, because a suite that
# quietly drops coverage reads as "everything passed" when it did not.
LLM = bool(os.environ.get("GROQ_API_KEY", "").strip())

skipped = []


def check(section, name, condition, detail=""):
    results.append((section, name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {name}"
    if not condition and detail:
        line += f"\n         -> {detail}"
    print(line, flush=True)


def skip(section, name, why="needs GROQ_API_KEY"):
    skipped.append((section, name, why))
    print(f"  [SKIP] {name}  ({why})", flush=True)


class Client:
    """A browser-like session."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            NoRedirect(),
        )

    def get(self, path):
        return self._send(urllib.request.Request(BASE + path))

    def csrf(self):
        """Fetch this session's CSRF token from the meta tag base.html emits.

        Re-fetched per POST rather than cached: logging in calls session.clear(),
        which rotates the token on purpose (a token minted before you signed in
        must not stay valid afterwards). A cached one would go stale exactly
        once per persona and fail in a way that looks like an app bug.
        """
        for probe in ("/dashboard", "/login", "/"):
            code, html, _ = self.get(probe)
            m = re.search(r'name="csrf-token" content="([^"]+)"', html)
            if code == 200 and m:
                return m.group(1)
        raise AssertionError("could not obtain a CSRF token from any page")

    def post(self, path, fields=None, files=None, json_body=None, csrf=True):
        if csrf and json_body is None:
            fields = dict(fields or {})
            fields.setdefault("csrf_token", self.csrf())
        if json_body is not None:
            req = urllib.request.Request(
                BASE + path, json.dumps(json_body).encode(),
                {"Content-Type": "application/json"})
        elif files:
            boundary = uuid.uuid4().hex
            body = b""
            for k, v in (fields or {}).items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="{k}"\r\n\r\n{v}\r\n').encode()
            for k, (fname, content) in files.items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="{k}"; filename="{fname}"\r\n'
                         f"Content-Type: application/octet-stream\r\n\r\n").encode()
                body += content + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(
                BASE + path, body,
                {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            req = urllib.request.Request(
                BASE + path, urllib.parse.urlencode(fields or {}).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"})
        return self._send(req)

    def _send(self, req):
        try:
            r = self.opener.open(req, timeout=90)
            return r.getcode(), r.read().decode("utf-8", "replace"), r.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location", "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def ask(slug, question):
    c = Client()
    code, body, _ = c.post(f"/api/c/{slug}/chat", json_body={"question": question})
    try:
        return code, json.loads(body)
    except Exception:
        return code, {}


# ───────────────────────────── 1. first-time visitor
print("\n1. VISITOR — never seen the site")
v = Client()
code, html, _ = v.get("/")
check("visitor", "landing page loads", code == 200, f"got {code}")
check("visitor", "explains what it does", "knows" in html and "documents" in html.lower())
check("visitor", "has signup call to action", "/signup" in html)
check("visitor", "links a live demo", "/c/pizza-palace" in html)
check("visitor", "404 on unknown page", v.get("/no-such-page")[0] == 404)
check("visitor", "404 on unknown workspace", v.get("/c/does-not-exist")[0] == 404)
check("visitor", "cannot reach dashboard", v.get("/dashboard")[0] == 302)
check("visitor", "cannot reach settings", v.get("/dashboard/settings")[0] == 302)
check("visitor", "cannot reach unanswered list", v.get("/dashboard/gaps")[0] == 302)

# ───────────────────────────── 2. signing up
print("\n2. NEW OWNER — signup validation")
s = Client()
bad = [
    ({"company_name": "Acme Books", "email": "nope", "password": "password123",
      "confirm": "password123"}, "valid email"),
    ({"company_name": "A", "email": "a@b.dev", "password": "password123",
      "confirm": "password123"}, "2-100 characters"),
    ({"company_name": "Acme Books", "email": "a@b.dev", "password": "short",
      "confirm": "short"}, "at least 8"),
    ({"company_name": "Acme Books", "email": "a@b.dev", "password": "password123",
      "confirm": "different1"}, "match"),
]
for fields, expect in bad:
    code, html, _ = s.post("/signup", fields)
    check("signup", f"rejects bad input ({expect})", expect in html, html[:120])

SUFFIX = uuid.uuid4().hex[:6]
EMAIL = f"owner-{SUFFIX}@acmebooks.test"
code, html, loc = s.post("/signup", {
    "company_name": "Acme Books", "email": EMAIL,
    "password": "password123", "confirm": "password123"})
check("signup", "valid signup succeeds", code == 302, f"{code} {html[:100]}")
check("signup", "sent to onboarding", "/onboarding" in loc, loc)

code, html, _ = Client().post("/signup", {
    "company_name": "Copycat", "email": EMAIL,
    "password": "password123", "confirm": "password123"})
check("signup", "duplicate email rejected", "already exists" in html)

# ───────────────────────────── 3. onboarding
print("\n3. NEW OWNER — onboarding wizard")
code, html, _ = s.get("/onboarding")
check("onboarding", "wizard loads", code == 200 and "Set up your bot" in html)
check("onboarding", "shows the future bot address", "/c/acme-books" in html)

def _make_png(w=96, h=96):
    import zlib, struct
    raw = b"".join(b"\x00" + bytes([124, 58, 237, 255] * w) for _ in range(h))
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

PNG = _make_png()
code, html, _ = s.post("/onboarding",
    fields={"company_name": "Acme Books",
            "company_tagline": "Ask about our books, hours and orders",
            "brand_color": "#7c3aed",
            "support_phone": "+44 20 7946 0100",
            "support_email": "help@acmebooks.test",
            "title": "faq",
            "text": ("Opening hours:\nWe open 9am and close 7pm Monday to Saturday, "
                     "and 11am to 5pm on Sunday.\n\n"
                     "Delivery:\nStandard delivery is 3 pounds and takes 2 to 4 days. "
                     "Orders over 25 pounds ship free.\n\n"
                     "Returns:\nReturn any book within 30 days for a full refund, "
                     "provided it is unread and undamaged.")},
    files={"logo": ("logo.png", PNG)})
check("onboarding", "completes and redirects", code == 302, f"{code} {html[:150]}")

# ───────────────────────────── 4. owner dashboard
print("\n4. OWNER — dashboard")
for path, label in [("/dashboard", "documents"), ("/dashboard/settings", "settings"),
                    ("/dashboard/gaps", "unanswered"), ("/dashboard/profile", "profile")]:
    code, html, _ = s.get(path)
    check("dashboard", f"{label} page loads", code == 200, f"got {code}")

code, html, _ = s.get("/dashboard")
check("dashboard", "lists the uploaded document", "faq.txt" in html)
check("dashboard", "shows own business name", "Acme Books" in html)

# The app appends -2, -3 ... when a slug is taken, so read the real one back
# rather than assuming. Hardcoding it made a re-run silently talk to the
# previous run's workspace.
m = re.search(r"/c/([a-z0-9-]+)", html)
SLUG_A = m.group(1) if m else "acme-books"
check("dashboard", "workspace has its own address", bool(m), html[:150])

# The logo is stored in the database, not on disk, so it must survive a
# redeploy. An earlier refactor silently dropped the upload call in
# onboarding and every other test still passed — hence this check.
code, body, _ = Client()._send(urllib.request.Request(f"{BASE}/c/{SLUG_A}/logo"))
check("dashboard", "uploaded logo is stored and served", code == 200, f"got {code}")
code, html2, _ = Client().get(f"/c/{SLUG_A}")
check("dashboard", "chat page shows the logo image", "/logo" in html2, "fell back to initial")

code, html, _ = s.post("/dashboard", {
    "title": "gift-cards",
    "text": "Gift cards:\nGift cards are available in 10, 25 and 50 pound values and never expire."})
check("dashboard", "can add a second document", "rebuilt the search index" in html)

code, html, _ = s.get("/dashboard")
check("dashboard", "second document listed", "gift-cards.txt" in html)

print("\n5. CUSTOMER — chatting with Acme Books")
if not LLM:
    for _name in ("answers from docs", "answers price", "uses the new document",
                  "greets", "cites sources", "refuses what docs don't cover",
                  "no bogus citation on refusal", "off-topic stays in scope",
                  "escalates to a real person", "stays calm under abuse",
                  "isolation: neither bot answers the other's question"):
        skip("customer", _name)
else:
  time.sleep(1)
  cases = [
      ("what time do you close", ["7pm", "7 pm", "19:00"], True, "answers from docs"),
      ("how much is delivery", ["3", "three"], True, "answers price"),
      ("how much are gift cards", ["10", "25", "50"], True, "uses the new document"),
      ("hi", ["help", "hi", "hello"], False, "greets"),
  ]
  for q, expect, want_source, label in cases:
      code, data = ask(SLUG_A, q)
      ans = data.get("answer", "").lower()
      hit = any(e.lower() in ans for e in expect)
      check("customer", f"{label}: {q!r}", code == 200 and hit, data.get("answer", "")[:120])
      if want_source:
          check("customer", f"cites a source for {q!r}", data.get("sources"), str(data.get("sources")))
      time.sleep(1.5)

  code, data = ask(SLUG_A, "do you sell laptops")
  ans = data.get("answer", "").lower()
  check("customer", "refuses what docs don't cover",
        "acme" in ans or "don't" in ans or "not" in ans, data.get("answer", ""))
  check("customer", "no bogus citation on refusal", not data.get("sources"), str(data.get("sources")))
  time.sleep(1.5)

  code, data = ask(SLUG_A, "what is the capital of France")
  check("customer", "off-topic: does not leak world knowledge",
        "paris" not in data.get("answer", "").lower(), data.get("answer", ""))
  time.sleep(1.5)

  code, data = ask(SLUG_A, "I want to talk to a human")
  ans = data.get("answer", "")
  check("customer", "escalates to a real person",
        "7946" in ans or "help@acmebooks.test" in ans, ans)
  time.sleep(1.5)

  code, data = ask(SLUG_A, "you are useless")
  ans = data.get("answer", "").lower()
  check("customer", "stays calm under abuse", "hello" not in ans[:8], data.get("answer", ""))

print("\n6. EDGE CASES")
code, data = ask(SLUG_A, "")
check("edge", "empty question rejected", code == 400, f"got {code}")
code, data = ask(SLUG_A, "x" * 1500)
check("edge", "over-long question rejected", code == 400, f"got {code}")
code, _ = ask("no-such-business", "hello")
check("edge", "chat to unknown workspace 404s", code == 404, f"got {code}")

code, html, _ = s.post("/dashboard", {"title": "", "text": ""})
check("edge", "empty document rejected", "Provide either" in html)
code, html, _ = s.post("/dashboard", {"title": "notes", "text": "   "})
check("edge", "whitespace-only document rejected",
      "Provide either" in html or "empty" in html)

print("\n7. SETTINGS + PROFILE")
code, html, _ = s.post("/dashboard/settings", {
    "company_name": "Acme Books", "support_phone": "6684889900-----8890000---88",
    "support_email": "", "brand_color": "#7c3aed"})
check("settings", "garbage phone rejected", "look like a real number" in html)

code, html, _ = s.post("/dashboard/settings", {
    "company_name": "Acme Books", "support_phone": "", "support_email": "bad",
    "brand_color": "#7c3aed"})
check("settings", "invalid email rejected", "valid email" in html)

code, html, _ = s.post("/dashboard/settings", {
    "company_name": "Acme Books", "company_tagline": "Books for everyone",
    "support_phone": "+44 20 7946 0100", "support_email": "help@acmebooks.test",
    "brand_color": "#16a34a"})
check("settings", "valid settings save", "Settings saved" in html)

code, html, _ = Client().get(f"/c/{SLUG_A}")
check("settings", "branding reaches the public bot",
      "Books for everyone" in html and "#16a34a" in html)

code, html, _ = s.post("/dashboard/profile", {
    "form_type": "password", "current_password": "WRONG",
    "new_password": "newpassword1", "confirm_password": "newpassword1"})
check("profile", "wrong current password rejected", "incorrect" in html)

code, html, _ = s.post("/dashboard/profile", {
    "form_type": "password", "current_password": "password123",
    "new_password": "newpassword1", "confirm_password": "newpassword1"})
check("profile", "password change works", "Password updated" in html)

fresh = Client()
code, _, _ = fresh.post("/login", {"email": EMAIL, "password": "password123"})
check("profile", "old password no longer works", code == 200, f"got {code}")
code, _, loc = fresh.post("/login", {"email": EMAIL, "password": "newpassword1"})
check("profile", "new password works", code == 302, f"got {code}")

print("\n8. UNANSWERED LIST")
code, html, _ = s.get("/dashboard/gaps")
check("gaps", "the page loads for the owner", code == 200, f"got {code}")
if not LLM:
    # Which questions land here is decided by the LLM's own classification, so
    # with no key there is nothing to assert about the contents.
    for _name in ("logs the real gap", "does not log greetings",
                  "does not log off-topic trivia", "mark done works"):
        skip("gaps", _name)
else:
    check("gaps", "logs the real gap", "laptops" in html.lower(),
          "expected 'do you sell laptops'")
    check("gaps", "does not log greetings", ">hi<" not in html)
    check("gaps", "does not log off-topic trivia", "capital of France" not in html)

    m = re.search(r"/dashboard/gaps/(\d+)/resolve", html)
    if m:
        code, _, _ = s.post(f"/dashboard/gaps/{m.group(1)}/resolve")
        check("gaps", "mark done works", code == 302, f"got {code}")
    else:
        check("gaps", "mark done works", False, "no resolve button found")

print("\n9. SECOND BUSINESS — isolation")
b = Client()
EMAIL2 = f"owner-{SUFFIX}@zenspa.test"
b.post("/signup", {"company_name": "Zen Spa", "email": EMAIL2,
                   "password": "password123", "confirm": "password123"})
b.post("/onboarding", fields={
    "company_name": "Zen Spa", "company_tagline": "Massage and facials",
    "brand_color": "#0ea5e9", "support_phone": "+44 20 7946 0999",
    "support_email": "hello@zenspa.test", "title": "faq",
    "text": ("Treatments:\nA 60 minute massage costs 70 pounds. A facial costs 55 pounds.\n\n"
             "Booking:\nBook online or call. Cancel free up to 24 hours before.")})

code, html, _ = b.get("/dashboard")
m = re.search(r"/c/([a-z0-9-]+)", html)
SLUG_B = m.group(1) if m else "zen-spa"
check("isolation", "second owner sees only own business",
      "Zen Spa" in html and "Acme Books" not in html)
check("isolation", "second owner sees only own documents", "gift-cards.txt" not in html)

code, html, _ = s.get("/dashboard")
check("isolation", "first owner unaffected",
      "Acme Books" in html and "Zen Spa" not in html)

if not LLM:
    skip("isolation", "spa bot cannot see bookshop docs")
    skip("isolation", "bookshop bot cannot see spa docs")
else:
    time.sleep(1)
    code, data = ask(SLUG_B, "how much are gift cards")
    check("isolation", "spa bot cannot see bookshop docs",
          "10" not in data.get("answer", "") or not data.get("sources"),
          data.get("answer", ""))
    time.sleep(1.5)
    code, data = ask(SLUG_A, "how much is a massage")
    check("isolation", "bookshop bot cannot see spa docs",
          "70" not in data.get("answer", ""), data.get("answer", ""))

code, html, _ = b.get("/dashboard/gaps")
check("isolation", "gap lists are separate", "laptops" not in html.lower())

print("\n10. PASSWORD RESET + EMAIL VERIFICATION")
import db as _db

owner = _db.get_user_by_email(EMAIL)
check("auth", "new account starts unverified", owner["email_verified"] == 0)

with _db.connection() as _cur:
    vrow = _cur.execute(
        "SELECT token FROM tokens WHERE user_id = ? AND purpose = 'verify'", (owner["id"],)
    ).fetchone()
check("auth", "signup issued a verification token", bool(vrow))

if vrow:
    code, html, _ = Client().get(f"/verify-email/{vrow['token']}")
    check("auth", "verification link confirms the account", "Email confirmed" in html)
    owner = _db.get_user_by_email(EMAIL)
    check("auth", "email_verified flips to true", owner["email_verified"] == 1)
    code, html, _ = Client().get(f"/verify-email/{vrow['token']}")
    check("auth", "verification link is single-use", "Link expired" in html)

# password reset
code, html, _ = Client().post("/forgot-password", {"email": "nobody-here@nowhere.test"})
check("auth", "unknown email gives the same message (no enumeration)",
      "on its way" in html)

code, html, _ = Client().post("/forgot-password", {"email": EMAIL})
check("auth", "known email triggers a reset link", "on its way" in html)

reset_owner = _db.get_user_by_email(EMAIL)
with _db.connection() as _cur:
    rrow = _cur.execute(
        "SELECT token FROM tokens WHERE user_id = ? AND purpose = 'reset'",
        (reset_owner["id"],),
    ).fetchone()
check("auth", "reset request issued a token", bool(rrow))

if rrow:
    RESET_TOKEN = rrow["token"]
    code, html, _ = Client().get(f"/reset-password/{RESET_TOKEN}")
    check("auth", "valid reset token shows the form", "Set a new password" in html)

    code, html, _ = Client().get("/reset-password/not-a-real-token")
    check("auth", "garbage token rejected on GET, before typing anything",
          "Link expired" in html)

    code, html, _ = Client().post(f"/reset-password/{RESET_TOKEN}",
        {"new_password": "aaaaaaaa", "confirm_password": "bbbbbbbb"})
    check("auth", "mismatched confirmation rejected", "don" in html and "match" in html)

    code, html, _ = Client().get(f"/reset-password/{RESET_TOKEN}")
    check("auth", "token survives a rejected mismatch (not burned early)",
          "Set a new password" in html)

    code, _, loc = Client().post(f"/reset-password/{RESET_TOKEN}",
        {"new_password": "newpassword9", "confirm_password": "newpassword9"})
    check("auth", "valid reset succeeds and logs in", code == 302 and "/dashboard" in loc)

    dead = Client()
    code, _, _ = dead.post("/login", {"email": EMAIL, "password": "password123"})
    check("auth", "old password dead after reset", code == 200)
    code, _, loc = dead.post("/login", {"email": EMAIL, "password": "newpassword9"})
    check("auth", "new password from reset works", code == 302 and "dashboard" in loc)

    code, html, _ = Client().get(f"/reset-password/{RESET_TOKEN}")
    check("auth", "reset token is single-use", "Link expired" in html)

# resend-verification cooldown
resend_owner = _db.get_user_by_email(EMAIL2)
if resend_owner:
    with _db.connection() as _cur:
        _cur.execute("DELETE FROM tokens WHERE user_id = ? AND purpose = 'verify'",
                     (resend_owner["id"],))
    b.post("/resend-verification", {})
    with _db.connection() as _cur:
        n1 = len(_cur.execute(
            "SELECT id FROM tokens WHERE user_id = ? AND purpose = 'verify'",
            (resend_owner["id"],)).fetchall())
    b.post("/resend-verification", {})
    with _db.connection() as _cur:
        n2 = len(_cur.execute(
            "SELECT id FROM tokens WHERE user_id = ? AND purpose = 'verify'",
            (resend_owner["id"],)).fetchall())
    check("auth", "resend cooldown blocks an immediate second click", n1 == 1 and n2 == 1)

# login throttle — progressive backoff, not lockout (a full lockout is itself
# a weapon: anyone could lock out a real user just by guessing wrong on
# purpose). Verifies both the per-account and per-IP layers independently.
print("\n11. LOGIN THROTTLE")


def _clear_attempts(where=""):
    with _db.connection() as _cur:
        _cur.execute("DELETE FROM login_attempts" + where)


def _login(email, password="wrong"):
    """Returns (status, body). A throttled attempt answers 429 immediately.

    These checks used to measure elapsed *time*, because the throttle slept
    through its backoff inside the request. It no longer does — sleeping held
    a server thread, so four wrong passwords at once froze the whole app — and
    asserting on a status code is both faster and not sensitive to how far
    away the database happens to be.
    """
    code, html, _ = Client().post("/login", {"email": email, "password": password})
    return code, html


# _backoff_seconds() returns 0 while fail_count <= 3, and each attempt reads
# the count *before* recording its own failure. So attempts 1-4 all get through
# and the 5th is the first to be refused.
_clear_attempts()
for n in range(4):
    code, _ = _login("throttle-test@nowhere.dev")
    check("throttle", f"failure {n + 1} is not throttled", code == 200, f"got {code}")

code, html = _login("throttle-test@nowhere.dev")
check("throttle", "the 5th failure is refused, not delayed", code == 429, f"got {code}")
check("throttle", "the refusal says how long to wait", "Wait about" in html, html[:160])

check("throttle", "a throttled attempt never reveals whether the account exists",
      "Wrong email or password" not in html)

# Isolate the email layer: every local test client shares one real IP
# (127.0.0.1), so the failures above also built up the IP counter. Clearing it
# here tests email-based isolation on its own — the IP layer's cross-account
# effect is verified separately below, deliberately.
_clear_attempts(" WHERE key LIKE 'ip:%'")

code, _, loc = Client().post("/login",
                             {"email": "demo@pizzapalace.example", "password": "demo12345"})
check("throttle", "an unrelated account is unaffected by another account's failures",
      code == 302, f"got {code}")

_clear_attempts(" WHERE key LIKE 'ip:%'")

for i in range(4):
    Client().post("/login", {"email": f"spray-{i}-{SUFFIX}@nowhere.dev", "password": "guess"})
code, _ = _login(f"spray-new-{SUFFIX}@nowhere.dev", "guess")
check("throttle", "spraying distinct emails from one IP still gets throttled",
      code == 429, f"5th distinct email got {code}, expected 429")

# A correct password after being throttled must still be refused — otherwise
# the throttle is only an inconvenience to someone guessing, not a control.
_clear_attempts()
for _ in range(4):
    Client().post("/login", {"email": "demo@pizzapalace.example", "password": "wrong"})
code, _, loc = Client().post("/login",
                             {"email": "demo@pizzapalace.example", "password": "demo12345"})
check("throttle", "even the right password is refused while throttled", code == 429,
      f"got {code} {loc}")

_clear_attempts()
code, _, loc = Client().post("/login",
                             {"email": "demo@pizzapalace.example", "password": "demo12345"})
check("throttle", "clearing the counter lets the right password back in", code == 302,
      f"got {code}")

print("\n12. CHAT RATE LIMIT")
# The public chat endpoint is unauthenticated and every call costs an LLM
# completion on a key shared by every tenant, so a loop against one known slug
# would drain the quota for all of them. Driven through db directly rather than
# by sending 60 real questions — that would be a minute of LLM calls to prove a
# counter, and the counter is the thing under test.
_db.clear_rate_limits()

IP_LIMIT, IP_WINDOW = 60, 60
probe = f"chat-ip:probe-{SUFFIX}"
allowed = sum(0 if _db.rate_limit_exceeded(probe, IP_LIMIT, IP_WINDOW) else 1
              for _ in range(IP_LIMIT + 10))
check("ratelimit", "allows exactly the limit, then refuses", allowed == IP_LIMIT,
      f"allowed {allowed}, expected {IP_LIMIT}")

check("ratelimit", "a different source is unaffected",
      not _db.rate_limit_exceeded(f"chat-ip:other-{SUFFIX}", IP_LIMIT, IP_WINDOW))

# Window rollover: rewind this key's window past its length and it starts fresh.
with _db.connection() as _cur:
    _cur.execute("UPDATE rate_limits SET window_start = ? WHERE key = ?",
                 ((datetime.now(timezone.utc) - timedelta(seconds=IP_WINDOW + 5)).isoformat(),
                  probe))
check("ratelimit", "the window rolls over and allows again",
      not _db.rate_limit_exceeded(probe, IP_LIMIT, IP_WINDOW))

# And the endpoint itself actually refuses once the counter is spent.
_db.clear_rate_limits()
with _db.connection() as _cur:
    _cur.execute("INSERT INTO rate_limits (key, window_start, hits) VALUES (?, ?, ?)",
                 (f"chat-tenant:{_db.get_tenant_by_slug(SLUG_A)['id']}",
                  datetime.now(timezone.utc).isoformat(), 99999))
code, data = ask(SLUG_A, "what are your opening hours?")
check("ratelimit", "the endpoint refuses with 429, not 500", code == 429, f"got {code}")
check("ratelimit", "and says so in a way a widget can show",
      "busy" in (data.get("error") or "").lower(), str(data)[:120])

_db.clear_rate_limits()
code, data = ask(SLUG_A, "what are your opening hours?")
check("ratelimit", "clearing the counter restores service", code == 200, f"got {code}")

print("\n13. CSRF")
# SESSION_COOKIE_SAMESITE="Lax" blocks the classic cross-site auto-submitted
# form in a current browser, but that is a browser behaviour, not a control
# this app enforces. These check the app's own guard.
csrf_client = Client()
csrf_client.post("/login", {"email": EMAIL2, "password": "password123"})

code, html, _ = csrf_client.post("/dashboard/settings",
                                 {"company_name": "CSRF Probe"}, csrf=False)
check("csrf", "a form POST with no token is rejected", code == 400, f"got {code}")
check("csrf", "and says nothing was changed", "Nothing was changed" in html, html[:200])
_, html, _ = csrf_client.get("/dashboard/settings")
check("csrf", "the rejected change really did not apply", "CSRF Probe" not in html)

code, _, _ = csrf_client.post("/dashboard/settings",
                              {"company_name": "Zen Spa", "csrf_token": "wrong-token"},
                              csrf=False)
check("csrf", "a wrong token is rejected too", code == 400, f"got {code}")

code, html, _ = csrf_client.post("/dashboard/settings",
                                 {"company_name": "Zen Spa", "support_phone": "",
                                  "support_email": "hello@zenspa.test",
                                  "brand_color": "#0ea5e9"})
check("csrf", "the real form still works", "Settings saved" in html, html[:200])

# One session's token must not authorise another's request, or the check is
# only proving that *a* token was present.
other = Client()
other.post("/login", {"email": EMAIL, "password": "newpassword9"})
code, _, _ = csrf_client.post("/dashboard/settings",
                              {"company_name": "Stolen", "csrf_token": other.csrf()},
                              csrf=False)
check("csrf", "another session's token does not work", code == 400, f"got {code}")

# The public chat API is exempt on purpose: no cookie auth, nothing to ride on.
code, data = ask(SLUG_A, "what are your opening hours?")
check("csrf", "the chat API stays exempt", code == 200, f"got {code}")

check("csrf", "every form in the app carries a token", all(
    open(f"templates/{t}").read().count("<form") ==
    open(f"templates/{t}").read().count('name="csrf_token"')
    for t in os.listdir("templates")
    if t.endswith(".html") and t != "chat.html"), "a form is missing its token")

print("\n14. RELIABILITY")
# These exercise rag/db directly rather than over HTTP: they are about what
# happens with four gunicorn threads and a long-lived process, which a
# sequential HTTP client cannot reproduce.
import threading  # noqa: E402

import ingest as _ingest  # noqa: E402
import rag as _rag  # noqa: E402

_t = _db.get_tenant_by_slug("pizza-palace")
_v0 = _t["index_version"]
_db.save_document(_t["id"], "e2e-version-probe.txt", "Parking:\nFree for 90 minutes.\n")
_v1 = _db.get_tenant_by_slug("pizza-palace")["index_version"]
check("reliability", "saving a document bumps index_version", _v1 == _v0 + 1, f"{_v0}->{_v1}")
_db.delete_document(_t["id"], "e2e-version-probe.txt")
check("reliability", "deleting bumps it too",
      _db.get_tenant_by_slug("pizza-palace")["index_version"] == _v1 + 1)

# A version bump made by another process must not be served from this one's
# cache — the whole reason invalidation is not a plain in-process call.
_t = _db.get_tenant_by_slug("pizza-palace")
_ingest.build_index(_t["id"], _t["slug"])
_rag.reload_index(_t["slug"])
_rag.search(_t, "opening hours")
_cached_version = _rag._indexes["pizza-palace"][2]
_db.save_document(_t["id"], "e2e-stale-probe.txt", "Wifi:\nPassword is guest123.\n")
_fresh = _db.get_tenant_by_slug("pizza-palace")
check("reliability", "a newer index_version misses the cached entry",
      _rag._cached("pizza-palace", _fresh["index_version"]) is None)
_db.delete_document(_t["id"], "e2e-stale-probe.txt")
_ingest.build_index(_t["id"], _t["slug"])
_rag.reload_index(_t["slug"])

_saved = dict(_rag._indexes)
_rag._indexes.clear()
for _i in range(_rag.MAX_CACHED_INDEXES + 5):
    _rag._remember(f"e2e-slug-{_i}", object(), [], 0)
check("reliability", f"the index cache is bounded at {_rag.MAX_CACHED_INDEXES}",
      len(_rag._indexes) == _rag.MAX_CACHED_INDEXES, f"held {len(_rag._indexes)}")
check("reliability", "least recently used entries are evicted",
      "e2e-slug-0" not in _rag._indexes)
_rag._indexes.clear()
_rag._indexes.update(_saved)

# Two threads asking a cold tenant the same question used to both rebuild, one
# writing index.faiss while the other read it.
_t = _db.get_tenant_by_slug("pizza-palace")
_rag.reload_index(_t["slug"])
for _p in (_ingest.index_path(_t["slug"]), _ingest.chunks_path(_t["slug"])):
    if os.path.exists(_p):
        os.remove(_p)
_builds, _real_build = [], _ingest.build_index
_ingest.build_index = lambda *a, **k: (_builds.append(1), _real_build(*a, **k))[1]
_hits = []
_threads = [threading.Thread(target=lambda: _hits.append(len(_rag.search(_t, "when do you open"))))
            for _ in range(6)]
for _x in _threads:
    _x.start()
for _x in _threads:
    _x.join()
_ingest.build_index = _real_build
check("reliability", "concurrent cold starts all get an answer", all(h > 0 for h in _hits), str(_hits))
check("reliability", "and the index is rebuilt exactly once", len(_builds) == 1,
      f"{len(_builds)} rebuilds")

check("reliability", "the LLM client has a timeout", _rag.LLM_TIMEOUT_SECONDS > 0)

_code, _body, _ = Client().get("/healthz")
_health = json.loads(_body) if _body.strip().startswith("{") else {}
check("reliability", "/healthz answers 200 when healthy", _code == 200, f"got {_code}")
check("reliability", "/healthz actually checks the database",
      _health.get("database") == "up", str(_health))
check("reliability", "/healthz names the storage backend and mail provider",
      _health.get("backend") in ("sqlite", "postgres") and "mail" in _health, str(_health))

# Pruning: dead rows go, live ones stay.
_u = _db.get_user_by_email("demo@pizzapalace.example")
_live = _db.create_token(_u["id"], "reset")
_now = datetime.now(timezone.utc)
with _db.connection() as _cur:
    _cur.execute("INSERT INTO tokens (user_id, token, purpose, expires_at, created_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (_u["id"], f"e2e-dead-{SUFFIX}", "verify",
                  (_now - timedelta(days=2)).isoformat(),
                  (_now - timedelta(days=3)).isoformat()))
check("reliability", "pruning removes expired rows", _db.prune_expired() >= 1)
check("reliability", "the expired token is gone",
      not _db.token_valid(f"e2e-dead-{SUFFIX}", "verify"))
check("reliability", "a live token survives pruning", _db.token_valid(_live, "reset"))

try:
    _db.update_tenant(_t["id"], **{"company_name = 'x' WHERE 1=1 --": "y"})
    check("reliability", "update_tenant refuses an unknown column", False, "it was accepted")
except ValueError:
    check("reliability", "update_tenant refuses an unknown column", True)

print("\n15. SECURITY")
a = Client()
a.post("/login", {"email": EMAIL2, "password": "password123"})
code, _, _ = a.post("/dashboard/documents/..%2F..%2Fsample_docs%2Ffaq.txt/delete")
check("security", "path traversal on delete blocked", code in (302, 404), f"got {code}")

# Resolved from this file's own location, never hardcoded: an absolute path
# to somebody's machine silently becomes False the moment the repository is
# moved, and this is the assertion that proves the traversal above did not
# actually delete anything.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_FAQ = os.path.join(REPO_ROOT, "sample_docs", "faq.txt")
check("security", "sample docs untouched", os.path.exists(SAMPLE_FAQ), SAMPLE_FAQ)

code, html, _ = a.post("/dashboard/settings",
    fields={"company_name": "Zen Spa"},
    files={"logo": ("evil.png", b"<script>alert(1)</script>")})
check("security", "script disguised as png rejected", "must be a PNG" in html)

code, html, _ = a.post("/dashboard/settings",
    fields={"company_name": "Zen Spa"},
    files={"logo": ("x.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>')})
check("security", "svg upload rejected", "must be a PNG" in html)

code, html, _ = a.post("/dashboard", fields={"title": "notes"},
                       files={"file": ("notes.exe", b"MZ\x90\x00")})
check("security", "non-txt document rejected", "Only .txt" in html)

code, html, _ = a.post("/dashboard/settings",
    fields={"company_name": "Zen Spa"},
    files={"logo": ("broken.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)})
check("security", "corrupt image rejected", "corrupted" in html, html[:150])

xss = "<script>alert('xss')</script>"
a.post("/dashboard/settings", {"company_name": xss, "support_phone": "",
                               "support_email": "", "brand_color": "#0ea5e9"})
code, html, _ = Client().get(f"/c/{SLUG_B}")
check("security", "company name is escaped, not executed",
      "<script>alert('xss')</script>" not in html, "raw script tag rendered!")
a.post("/dashboard/settings", {"company_name": "Zen Spa", "support_phone": "",
                               "support_email": "hello@zenspa.test", "brand_color": "#0ea5e9"})

out = Client()
out.post("/login", {"email": EMAIL2, "password": "password123"})
out.get("/logout")
check("security", "logout ends the session", out.get("/dashboard")[0] == 302)

# ───────────────────────────── summary
print("\n" + "=" * 62)
by_section = {}
for section, name, ok, _ in results:
    p, f = by_section.get(section, (0, 0))
    by_section[section] = (p + (1 if ok else 0), f + (0 if ok else 1))
for section, (p, f) in by_section.items():
    status = "OK" if f == 0 else f"{f} FAILED"
    print(f"  {section:12} {p} passed  {status}")

total_p = sum(1 for *_, ok, _ in [(r[0], r[1], r[2], r[3]) for r in results] if ok)
total_f = len(results) - total_p
print("=" * 62)
print(f"TOTAL: {total_p} passed, {total_f} failed, {len(results)} checks")
if skipped:
    print(f"\n{len(skipped)} checks SKIPPED — coverage below what a full run gives:")
    for section, name, why in skipped:
        print(f"  [{section}] {name} ({why})")
if total_f:
    print("\nFailures:")
    for section, name, ok, detail in results:  # noqa
        if not ok:
            print(f"  [{section}] {name}")
            if detail:
                print(f"      {detail[:200]}")
sys.exit(1 if total_f else 0)   # skips are reported, not failed

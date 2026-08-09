# Implementation Plan — gap analysis and remediation

Audit of the repository as of commit `3328444`, and the work needed to close what
it found. Written against the actual code, not the roadmap: several items below
are already listed in [ROADMAP.md](ROADMAP.md) as deferred, several are not
recorded anywhere, and two are live defects.

**Scope of the audit:** `app.py`, `db.py`, `rag.py`, `ingest.py`, `mailer.py`,
`security.py`, `eval.py`, `seed_demo.py`, `tests/e2e.py`, `Dockerfile`,
`requirements.txt`, templates and static assets.

---

## Summary

| Severity | Count | Theme |
|---|---|---|
| **P0** — exploitable or actively broken | 6 | CSRF, cookie flags, chat-endpoint abuse, throttle DoS, secret key, dead test assertion |
| **P1** — will break under load or scale | 9 | Index cache races, no LLM timeout, synchronous re-index, unbounded tables, verification is dead code |
| **P2** — product and engineering debt | 19 | No CI, no unit tests, unpinned deps, no logging, no widget, no memory, no reranking |

Nothing here contradicts the design decisions already documented — the storage
model, the two-prompt split, the outcome-marker citation rule and the progressive
throttle are all sound and stay as they are.

---

## P0 — do these first

### P0-1. No CSRF protection on any state-changing route

**Where:** every `POST` in [app.py](app.py) — `/dashboard`, `/dashboard/settings`,
`/dashboard/profile`, `/dashboard/documents/<name>/delete`, `/dashboard/gaps/<id>/resolve`,
`/onboarding`, `/resend-verification`.

`SESSION_COOKIE_SAMESITE = "Lax"` ([app.py:44](app.py#L44)) blocks the classic
cross-site auto-submitted form in a current browser, and that is genuinely most
of the vector. It is not the whole of it: `Lax` does not cover a same-site
subdomain, a browser that ships a different default, or any future route that
accepts a `GET`-triggered side effect. ROADMAP records this as awaiting sign-off.
It is the single largest security gap in the app.

**Fix — do not add Flask-WTF.** It pulls WTForms and a form-class rewrite of
every template for a feature that is ~30 lines. Add a token helper instead:

```python
# security.py
import hmac, secrets

def issue_csrf_token(session):
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]

def csrf_ok(session, submitted):
    expected = session.get("csrf_token")
    return bool(expected) and hmac.compare_digest(expected, submitted or "")
```

```python
# app.py
@app.before_request
def _enforce_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    if request.path.startswith("/api/"):   # public chat API, no session, no cookie auth
        return
    if not csrf_ok(session, request.form.get("csrf_token")):
        abort(400)

@app.context_processor
def _inject_csrf():
    return {"csrf_token": issue_csrf_token(session)}
```

Then one line in every `<form>` in `templates/`:

```html
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
```

Templates to touch: `dashboard.html`, `settings.html`, `profile.html`,
`onboarding.html`, `gaps.html`, `login.html`, `signup.html`,
`forgot_password.html`, `reset_password.html`, `_dash_nav.html` (logout is a
`GET` link today — leave it, or convert it to a `POST` and include the token).

`/api/c/<slug>/chat` is deliberately exempt: it is unauthenticated, carries no
ambient credential, and a CSRF token on it would only break the embed widget
planned in P2-1.

**Verify:** an e2e check that a POST to `/dashboard/settings` without a token
returns 400, and that the normal form flow still returns 302.

---

### P0-2. Session cookie is missing `Secure`, and `SECRET_KEY` silently defaults to random

**Where:** [app.py:42-44](app.py#L42-L44).

```python
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
```

Two distinct problems:

- The session cookie has no `Secure` flag, so it is transmitted over plain HTTP
  if anything ever reaches the container without TLS, and no explicit `HttpOnly`
  or lifetime.
- If `SECRET_KEY` is unset in production the app boots happily with a random key.
  Every restart silently logs every user out, and the moment `--workers` goes
  above 1 each worker signs sessions with a *different* key, so logins fail
  non-deterministically depending on which worker answers. Failing loudly at
  boot is strictly better than a heisenbug.

**Fix:**

```python
IS_PRODUCTION = bool(os.environ.get("DATABASE_URL", "").strip())

secret = os.environ.get("SECRET_KEY", "").strip()
if not secret:
    if IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY must be set in production — sessions cannot be signed.")
    secret = os.urandom(24)          # local dev only
app.secret_key = secret

app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)
```

Gate `SESSION_COOKIE_SECURE` on production rather than hardcoding `True`, or
local development over `http://localhost:5001` stops being able to log in.

---

### P0-3. The public chat endpoint has no rate limit

**Where:** [app.py:457](app.py#L457).

`POST /api/c/<slug>/chat` is public, unauthenticated, and every call costs one
Groq completion — two when retrieval misses, because `_classify_message()` fires
a second request ([rag.py:348](rag.py#L348)). A loop of `curl` against a known
slug (`pizza-palace` is in the README) will exhaust the free-tier token budget
for *every tenant on the deployment*, since they share one API key. That is a
denial of service against paying-customer-facing bots, and it costs the attacker
nothing.

**Fix — per-IP and per-tenant token bucket in the existing database.** Reuse the
`login_attempts` pattern rather than adding Redis; the request volume this app
handles does not justify another service.

```python
# db.py
CHAT_RATE_LIMITS = [
    ("ip",     30,  60),      # 30 questions / minute from one address
    ("tenant", 600, 3600),    # 600 questions / hour for one workspace
]

def rate_limit_exceeded(key, limit, window_seconds):
    """Fixed-window counter. Returns True if this call should be rejected."""
    ...
```

```python
# app.py, inside chat_api()
if db.rate_limit_exceeded(f"ip:{request.remote_addr}", 30, 60):
    return jsonify({"error": "Too many questions — please slow down."}), 429
if db.rate_limit_exceeded(f"tenant:{tenant['id']}", 600, 3600):
    return jsonify({"error": "This assistant is busy — try again shortly."}), 429
```

A fixed window is deliberately chosen over a sliding one: it is one row and one
UPDATE, the boundary-burst weakness is irrelevant at these limits, and the
alternative costs more than the abuse it prevents.

Return `429`, not `400` — the widget should be able to distinguish "slow down"
from "malformed" and back off.

**Verify:** e2e loop of 40 rapid questions; expect a `429` before the 40th and
a normal `200` after the window rolls.

---

### P0-4. The login throttle blocks a worker thread

**Where:** [app.py:159-161](app.py#L159-L161).

```python
delay = max(db.get_login_delay(email_key), db.get_login_delay(ip_key))
if delay:
    time.sleep(delay)
```

The backoff is correct in design — progressive delay rather than lockout, with
the reasoning documented in [db.py:368-376](db.py#L368-L376) — but it is spent
**inside the request, holding a gunicorn thread.** Production runs
`--workers 1 --threads 4`. The cap is 20 seconds. Four concurrent failed logins
therefore park all four threads for 20 seconds each and the entire app —
dashboards, chat endpoints, every tenant — stops serving. An attacker needs four
sockets and no valid credentials.

**Fix: reject rather than sleep.** Keep the progressive schedule, but return
immediately with the wait remaining.

```python
delay = max(db.get_login_delay(email_key), db.get_login_delay(ip_key))
if delay:
    db.record_login_failure(email_key)        # keep the pressure escalating
    db.record_login_failure(ip_key)
    error = f"Too many attempts. Wait {delay} seconds and try again."
    return render_template("login.html", error=error, email=email), 429
```

This preserves every property the current design was chosen for: no lockout, the
account stays reachable, per-account and per-IP layers both still apply. It
removes the thread-exhaustion vector entirely. Cap the message granularity
(e.g. round to 5s) so the response does not become a precise oracle for how many
failures an unrelated party has racked up against an address.

Note the existing e2e throttle checks ([tests/e2e.py:455-490](tests/e2e.py#L455-L490))
measure *elapsed time*. They must be rewritten to assert on status code `429`
and the message, which is a faster and more deterministic test besides.

---

### P0-5. Response security headers are absent

**Where:** no `after_request` hook exists in [app.py](app.py).

`/c/<slug>/logo` serves attacker-supplied bytes from the application's own
origin. Content sniffing is defended against on the way *in* (magic bytes plus a
header-dimension parse, [app.py:517-579](app.py#L517-L579) — genuinely good), but
nothing tells the browser not to sniff on the way *out*, and the chat page has no
CSP.

**Fix:**

```python
@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.path.startswith("/c/") and "/widget" not in request.path:
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    else:
        response.headers.setdefault("X-Frame-Options", "DENY")
    if IS_PRODUCTION:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response
```

Leave the widget route (P2-1) out of `X-Frame-Options` deliberately — that route
exists to be framed. A CSP is worth adding to the chat page too, but note
`chat.html` uses an inline `<style>` for the brand colour and an inline
`<script>` for the endpoint, so a CSP needs either a nonce or those two moved out
first. Do the headers now; do the CSP with the widget work.

---

### P0-6. `tests/e2e.py` contains a hardcoded path from an old machine

**Where:** [tests/e2e.py:515](tests/e2e.py#L515).

```python
check("security", "sample docs untouched",
      os.path.exists("/Users/NI011/Desktop/AI Customer-Support Assistant (RAG)/sample_docs/faq.txt"))
```

The repository now lives at `.../Customer support Rag/ai-customer-support-rag`.
That directory does not exist, so this check **fails on every run** — and it is
the assertion guarding the path-traversal test directly above it. The suite's
advertised count is wrong and one security check is inert.

**Fix:**

```python
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
check("security", "sample docs untouched",
      os.path.exists(os.path.join(REPO_ROOT, "sample_docs", "faq.txt")))
```

Grep the suite for any other absolute path while in there. Then reconcile the
counts: README says both "73 checks" (line 85) and "71 checks" (line 195),
ROADMAP says "96 end-to-end checks" (line 25). Three numbers, one suite. Print
the real total and cite that.

---

## P1 — will break under load or as tenants grow

### P1-1. The index cache is not thread-safe and never evicts

**Where:** [rag.py:43](rag.py#L43), `_indexes = {}`.

Production is `--threads 4`. Two threads asking the same fresh tenant a question
simultaneously both miss the cache, both call `ingest.build_index()`, and both
write to the same `data/<slug>/index.faiss` — one `faiss.write_index()` racing
another `faiss.read_index()` on the same path yields a truncated read or a
corrupt index. The window is small but it is exactly the cold-start-after-deploy
path, i.e. the busiest moment.

Separately, the dict grows without bound. Each entry holds a FAISS index plus the
full chunk text in memory, and nothing is ever evicted — on a 512 MB instance
already sized tightly around the embedding model, that is a slow OOM as tenant
count rises.

**Fix:** a lock plus an LRU bound.

```python
import threading
from collections import OrderedDict

MAX_CACHED_INDEXES = 20
_indexes = OrderedDict()
_index_lock = threading.Lock()
_build_locks = {}          # slug -> Lock, so two tenants never block each other

def _lock_for(slug):
    with _index_lock:
        return _build_locks.setdefault(slug, threading.Lock())

def _load_index(tenant):
    slug = tenant["slug"]
    with _index_lock:
        if slug in _indexes:
            _indexes.move_to_end(slug)
            return _indexes[slug]
    with _lock_for(slug):                       # per-tenant build lock
        with _index_lock:                       # re-check: another thread may have built it
            if slug in _indexes:
                return _indexes[slug]
        loaded = _read_or_build(tenant)         # existing body, unchanged
        if loaded is None:
            return None
        with _index_lock:
            _indexes[slug] = loaded
            _indexes.move_to_end(slug)
            while len(_indexes) > MAX_CACHED_INDEXES:
                _indexes.popitem(last=False)
        return loaded
```

Per-tenant build locks matter: a single global build lock would make one tenant's
cold rebuild block every other tenant's chat request.

Write the index atomically as well — `faiss.write_index()` to a temp path in the
same directory, then `os.replace()` — so a reader can never observe a partial
file. Same for `chunks.pkl`.

### P1-2. `reload_index()` only invalidates the process that handled the upload

**Where:** [rag.py:80](rag.py#L80), called from [app.py:340](app.py#L340) and
[app.py:634](app.py#L634).

Correct today because there is exactly one worker. It becomes a silent
correctness bug the instant `--workers` goes above 1 or a second instance is
added: an owner uploads a document, and half the requests keep answering from the
stale index with no error anywhere.

**Fix:** stamp a version on the tenant and compare it on load, rather than
relying on in-process invalidation.

- Add `index_version INTEGER NOT NULL DEFAULT 0` to `tenants` (use the existing
  `_add_column_if_missing()` helper — it already handles both backends).
- `_save_document` / `delete_document` increment it.
- `_load_index()` caches `(index, chunks, version)` and rebuilds when the tenant
  row's version is newer.

That is one cheap `SELECT index_version` per chat request, which is far less than
the LLM call it precedes. Do this now even while single-worker — it is ten lines
today and an outage later.

### P1-3. No timeout on the Groq client

**Where:** [rag.py:104](rag.py#L104), `Groq(api_key=api_key)`.

No explicit timeout. A hung upstream connection holds a gunicorn thread until the
120s server timeout. With four threads, three hung calls plus one throttled login
is a full outage.

**Fix:** `Groq(api_key=api_key, timeout=20.0, max_retries=0)`. Retries are already
handled deliberately in `_complete_with_retry()` ([rag.py:274](rag.py#L274)) with
a documented budget — let the SDK's own retry layer stay off so the two do not
multiply into a 60s worst case.

### P1-4. Re-indexing is synchronous and rebuilds the whole corpus

**Where:** [app.py:633](app.py#L633) and [app.py:339](app.py#L339).

Every upload and every delete re-embeds *all* of that tenant's documents inline
in the request. Cost is O(total corpus), not O(changed document). One 2 MB
document is roughly 3,500 chunks through MiniLM on a single CPU thread — minutes,
inside an HTTP request, behind a 120s gunicorn timeout. ROADMAP notes this.

**Fix, in two steps:**

1. **Now, cheap:** move the rebuild off the request thread. A
   `concurrent.futures.ThreadPoolExecutor(max_workers=1)` with a per-tenant
   dedupe, plus an `indexing` flag on the tenant row so the dashboard can show
   "indexing…" and the chat page can say the bot is still learning. Returns the
   upload response immediately.
2. **Later, correct:** incremental indexing. Store a `doc_id` on each chunk,
   and on save re-embed only that document's chunks and rewrite the index. FAISS
   `IndexFlatIP` has no delete, so keep an `IndexIDMap2` over it and rebuild
   only when the tombstone ratio crosses a threshold.

Step 1 is the one that matters; step 2 only pays off past a few hundred
documents per tenant.

### P1-5. Email verification is written but never enforced — it is dead code

**Where:** `email_verified` is set at [app.py:200](app.py#L200) and read at
[app.py:211](app.py#L211), and **nowhere else**. The last two commits removed the
banner and the resend button, so `/resend-verification` is now unreachable from
any template.

The column, the token purpose, the mailer template, the route and four e2e checks
all exist to maintain a flag that gates nothing. Any user can sign up with an
address they do not control and use the product fully.

This is genuinely blocked on the outbound-email domain (ROADMAP records the eu.org
request as pending), so it is deliberate — but the current state is the worst of
both: the machinery is carried and the guarantee is not delivered.

**Fix, in the order the blocker allows:**

1. **Now:** make the state explicit rather than invisible. Reinstate a dismissible
   banner behind a `REQUIRE_EMAIL_VERIFICATION` env flag, defaulting off, so the
   feature can be switched on the day the domain lands without another code
   change. Restore the resend button under the same flag.
2. **Now:** fix the correctness bug alongside it — `update_user_email()`
   ([db.py:295](db.py#L295)) does not reset `email_verified`. A user verifies
   address A, changes to address B, and remains "verified" for an address nobody
   ever confirmed. Reset the flag and issue a fresh verification token in the same
   transaction.
3. **When the domain lands:** gate the public bot (`/c/<slug>`) — not the
   dashboard — on verification. Locking an owner out of their own dashboard over
   an email they may never receive is worse than the problem; refusing to serve a
   public bot for an unverified account is the actual anti-abuse control.

### P1-6. `update_tenant(**fields)` interpolates column names into SQL

**Where:** [db.py:510-516](db.py#L510-L516).

```python
columns = ", ".join(f"{key} = ?" for key in fields)
cur.execute(f"UPDATE tenants SET {columns} WHERE id = ?", [...])
```

Values are parameterised correctly. Keys are not — they are formatted straight
into the statement. Every current caller passes literal keyword arguments, so
this is not exploitable today. It is one `**request.form` away from being a
straightforward injection, and that refactor looks harmless when someone makes it.

**Fix:** allow-list the columns.

```python
TENANT_UPDATABLE = frozenset({
    "company_name", "company_tagline", "brand_color",
    "support_phone", "support_email", "onboarded", "index_version",
})

def update_tenant(tenant_id, **fields):
    unknown = set(fields) - TENANT_UPDATABLE
    if unknown:
        raise ValueError(f"Not updatable: {sorted(unknown)}")
    ...
```

### P1-7. `tokens` and `login_attempts` grow forever

**Where:** [db.py:224-242](db.py#L224-L242). ROADMAP notes `login_attempts`; the
same applies to `tokens`, where every verification and reset link is retained
indefinitely including consumed ones.

**Fix:** a `prune_expired()` in `db.py` deleting `tokens` past expiry and
`login_attempts` older than the backoff window, called opportunistically from
`init_db()` at boot plus roughly 1-in-200 logins. No scheduler needed at this
scale, and boot-time pruning alone would never fire on a long-lived instance.

### P1-8. Three database round trips per authenticated request

**Where:** `current_user()` ([app.py:65](app.py#L65)) is called by the
`inject_user` context processor, again by `login_required`, and `with_tenant`
adds a `get_tenant_for_user` on top. Against a remote Neon instance that is
three sequential network round trips before any page logic runs.

**Fix:** memoise on `flask.g` for the request's lifetime.

```python
def current_user():
    if "user" not in g:
        user_id = session.get("user_id")
        g.user = db.get_user(user_id) if user_id else None
    return g.user
```

Same treatment for the tenant lookup in `with_tenant`.

### P1-9. The Dockerfile bakes a SQLite database with a known-password account into the image

**Where:** [Dockerfile:31](Dockerfile#L31), `RUN python seed_demo.py`.

At build time `DATABASE_URL` is unset, so this writes `chatbot.db` — containing
`demo@pizzapalace.example` / `demo12345` — into the image layer. In production
the app reads Postgres and never touches that file, so the demo it was meant to
provide does not appear, and a credentialed SQLite file ships in every image.
(`.dockerignore` excludes `chatbot.db` from the build *context*; it does not stop
a `RUN` step creating one inside the image.)

**Fix:** drop the build-time seed. Seed at container start instead, guarded, so
it runs against whatever database is actually configured:

```dockerfile
CMD python -c "import seed_demo; seed_demo.seed()" ; \
    gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app
```

`seed_demo.seed()` is already idempotent. Gate it on a `SEED_DEMO=true` env var
so a real deployment can turn it off. Keep the model pre-download `RUN` — that one
is doing exactly what it claims.

---

## P2 — product and engineering debt

### P2-1. Embed widget *(ROADMAP item 1)*

The highest-value missing feature. A business currently has to send customers to
a separate URL, which almost no business will do.

**Build:** `/embed.js` serving a small launcher, `/c/<slug>/widget` serving a
compact chat page, and `X-Frame-Options` relaxed on that route only (see P0-5).
Iframe rather than shadow DOM, for the reason ROADMAP already records — CSS
isolation in both directions, and no CORS because the frame is same-origin to
itself.

Two things ROADMAP does not mention that this needs:
- The widget makes the chat API cross-origin-embedded, so **P0-3 rate limiting is
  a hard prerequisite** — a widget on a busy site is a traffic multiplier.
- `postMessage` for open/close sizing, with an explicit `targetOrigin`, never `*`.

### P2-2. Conversation memory *(README known limitation)*

`ask()` takes a single question and no history, so "how much?" after "do you have
Margherita?" cannot resolve. This is the most visible quality gap to an actual
customer.

**Approach:** pass the last N turns (N=4 is usually enough) to a cheap rewrite
call that resolves the pronoun into a standalone question, embed *that*, and give
the original to the generation prompt. Keep history client-side in the widget and
post it with the request — that preserves the "customer conversations are never
stored" property documented in [db.py:18-20](db.py#L18-L20), which is worth
keeping. Cap history length server-side so the payload cannot be used to inflate
prompt size.

Gate it behind `eval.py` — a rewrite step can hurt single-turn accuracy, and the
suite exists precisely to catch that.

### P2-3. Confidence scoring and reranking *(ROADMAP item 3)*

Unchanged from the roadmap's own analysis, which is sound. Sequence it as:
cross-encoder rerank first (biggest win, easiest to measure), then graduated
thresholds, then surfacing confidence in the dashboard. Measure each against
`eval.py` before keeping it. Note the memory constraint — a cross-encoder is a
second resident model on an instance already sized around the first.

### P2-4. No CI

No `.github/` directory. Both suites are run by hand, which is how P0-6 survived.

**Fix:** `.github/workflows/ci.yml` — ruff, then `pytest` (P2-5), then boot the
app against SQLite and run `tests/e2e.py`. `eval.py` cannot run in CI without a
Groq key; run it on a schedule with a repository secret, or gate it behind
`if: github.event_name == 'schedule'`.

### P2-5. No unit tests, and the e2e suite needs a live server and mutates real data

`tests/e2e.py` requires `python app.py` running on port 5001 and writes to
whatever database that process is pointed at — which is how the ROADMAP's "Neon
has leftover test data" note came about.

**Fix:** add `pytest` with `app.test_client()` fixtures and a temp-file SQLite
database per test session. Unit-test the pure functions first, since they carry
the most logic per line and need no network:

| Target | Why |
|---|---|
| `ingest.chunk_text` | Section-boundary chunking is the single biggest accuracy lever measured |
| `rag._split_outcome` | Silent mis-parse means wrong citations |
| `app._sniff_image` / `_image_dimensions` | Hand-rolled header parsers over untrusted bytes |
| `app._clean_support_phone` / `_clean_color` | Validation the e2e suite only samples |
| `db._question_key` | Determines whether gap dedupe works |
| `db.slugify` / `unique_slug` | Reserved words and collisions reach URLs and the filesystem |

Keep `tests/e2e.py` — it tests things a unit test cannot. Just stop it being the
only thing.

### P2-6. Dependencies are unpinned

[requirements.txt](requirements.txt) is all `>=`. Builds are not reproducible; a
`sentence-transformers` or `psycopg` minor release can change behaviour between
two deploys of identical source. The Dockerfile already asserts the torch build
is `+cpu`, which shows the concern is understood — extend it.

**Fix:** pin exact versions in `requirements.txt` (keep a `requirements.in` if you
want the loose spec), and add Dependabot so pinning does not become staleness.

### P2-7. `print()` instead of logging

Eight `print()` calls across [rag.py](rag.py) and [mailer.py](mailer.py) are the
entire observability story. No levels, no timestamps, no request correlation, no
way to silence one component.

**Fix:** stdlib `logging`, configured once in `app.py`, one `logger = logging.getLogger(__name__)`
per module. Keep writing to stdout — that is correct for containers. Add a request
id via `before_request` and include it in the chat-failure log line, so a
customer report can be traced to the LLM error that caused it.

### P2-8. No health endpoint

Nothing to point a platform health check or uptime monitor at. `/` renders a
template and does not touch the database, so it stays green while Postgres is
down.

**Fix:** `/healthz` returning `{"ok": true, "db": "up", "index_cache": n}` after a
`SELECT 1`. Exclude it from logging so it does not drown the log.

### P2-9. No account or workspace deletion, and no data export

A user can create an account and documents, and has no way to remove either. For
a product that stores a business's own content and their customers' questions,
that is a straightforward GDPR/DPA gap as well as a product one. The schema
already has `ON DELETE CASCADE` throughout, so the data layer is ready.

**Fix:** `/dashboard/profile` → delete account, password-confirmed, with a typed
confirmation. Delete the user row (cascade handles tenants, documents,
unanswered, tokens), then remove `data/<slug>/`. Add a JSON export of documents
and the gap list on the same page.

### P2-10. Smaller items

| Gap | Where | Fix |
|---|---|---|
| Gap list capped at 100 with no pagination | [db.py:620](db.py#L620) | Offset paging, or "showing 100 of N" |
| No answered-question analytics | `chat_api` | Count answered/refused per day per tenant — deflection rate is the metric that sells the product |
| `.txt` only | [app.py:613](app.py#L613) | Add `.md` (trivial), then PDF via `pypdf` |
| No `robots.txt` | — | Allow `/`, disallow `/c/` and `/dashboard` |
| 413 handler returns bare text | [app.py:643](app.py#L643) | Render the styled error template |
| No 500 handler | [app.py](app.py) | Add one; a stack trace to a customer on a branded bot page is not acceptable |
| Source filenames exposed to customers | [rag.py:342](rag.py#L342) | Owners will upload `internal-pricing.txt`. Show a display title, keep the filename internal |
| No LICENSE | — | Add one; a public repo without it is "all rights reserved" by default |
| `eval.py` is keyword-matching only | [eval.py](eval.py) | Add a retrieval metric (recall@k against known-correct chunks) — it isolates a retrieval regression from a generation regression, which the current score cannot |
| One workspace per account | [db.py:503](db.py#L503) | `get_tenant_for_user` already does `ORDER BY id LIMIT 1`; the schema supports many. Needs a workspace switcher, not a migration |

---

## Sequencing

Ordered so each phase is independently shippable and nothing depends on work that
has not landed.

### Phase 1 — Security and correctness (~1.5 days)
P0-6 (one line, unblocks trusting the suite) → P0-1 CSRF → P0-2 cookies and
secret → P0-4 throttle rejection → P0-3 rate limiting → P0-5 headers → P1-6
column allow-list.

**Done when:** e2e covers CSRF rejection, `429` on both throttle and chat flood,
and the suite's real check count is printed and matches the README.

### Phase 2 — Reliability (~2 days)
P1-1 index locking and LRU → P1-2 index versioning → P1-3 LLM timeout → P1-4
step 1 (async re-index) → P1-7 pruning → P1-8 request memoisation → P1-9
Dockerfile seed.

**Done when:** two concurrent cold-start requests to the same tenant produce one
rebuild and two correct answers; an upload returns in under a second regardless
of corpus size.

### Phase 3 — Engineering foundation (~1.5 days)
P2-5 pytest and fixtures → P2-4 CI → P2-6 pinning → P2-7 logging → P2-8 health.

**Done when:** CI is green on a PR and fails on a deliberately broken assertion.

### Phase 4 — Product (~1 week)
P2-1 embed widget (needs P0-3 and P0-5) → P2-9 deletion and export → P2-2
conversation memory → P2-3 reranking and confidence.

**Done when:** the widget loads on a third-party page, and `eval.py` has not
regressed from 100%.

---

## What not to change

Worth stating explicitly, because several of these look like gaps until the
reasoning is read:

- **The two-prompt split in `build_prompt()`.** The no-context prompt giving the
  model no room to answer is what stops the invented gluten-free menu item.
- **The outcome-marker citation rule.** Under-citing rather than mis-citing is
  the right default, and the branch-specific marker lists exist because a shared
  list caused the model to anchor on the first label.
- **Progressive throttle rather than lockout.** The reasoning in
  [db.py:368-376](db.py#L368-L376) is correct — only the `sleep()` needs to go,
  not the schedule.
- **PBKDF2 over scrypt.** A documented, deliberate memory trade, with the
  migration path preserved by `check_password_hash` reading the algorithm from
  the stored hash.
- **FAISS index as disposable derived data.** Correct for the hosting model, and
  the rebuild-on-miss path already works.
- **Not storing customer conversations.** Keep this through the P2-2 memory work
  — client-held history preserves it.

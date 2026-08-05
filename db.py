"""Storage layer. Runs on Postgres in production, SQLite locally.

Set DATABASE_URL to use Postgres (Neon, Supabase, Render — anywhere; the
database does not have to live with the web host). Without it, a local
SQLite file is used so development needs no setup.

Everything durable lives here, including document text and logo images.
On free hosting the container's filesystem is wiped on every deploy, so
anything kept only on disk would vanish. The FAISS index is the deliberate
exception: it is derived data, rebuilt from these tables when missing.

Tables:
  users       - people who sign up
  tenants     - one business workspace per user, plus its branding and logo
  documents   - the text each bot answers from (source of truth)
  unanswered  - questions the bot could not answer

Customer conversations are never stored. Only unanswered questions are kept,
because that is what the owner needs; a transcript would be a privacy
liability and would bury the signal under "hi" and "thanks".
"""

import os
import re
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = "chatbot.db"

# Slugs end up in URLs and on the filesystem, so keep them strictly safe.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
RESERVED_SLUGS = {
    "admin", "api", "login", "signup", "logout", "dashboard", "static",
    "settings", "profile", "documents", "c", "help", "about", "new", "gaps",
}

_pool = None


def _connect():
    if not USE_POSTGRES:
        import sqlite3
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    global _pool
    if _pool is None:
        import atexit

        from psycopg_pool import ConnectionPool
        # Small pool: one web worker, and free-tier Postgres caps connections.
        # Pooling matters here because the database is remote — reconnecting per
        # request would add a TLS handshake to every page load.
        _pool = ConnectionPool(
            DATABASE_URL, min_size=1, max_size=4, open=True,
            # Be explicit rather than inheriting whatever the server defaults
            # to: a SQL_ASCII database hands back bytes instead of str.
            kwargs={"client_encoding": "UTF8"},
            # Neon (and similar serverless Postgres) suspends its compute
            # after a few minutes idle, silently dropping every open
            # connection. Without this, the pool hands out one of those dead
            # connections and the request dies with "AdminShutdown" or "the
            # connection is lost". check_connection pings with SELECT 1 before
            # handing a connection out and transparently reconnects if it's
            # gone, so a request after idle time costs one extra round trip
            # instead of a 500.
            check=ConnectionPool.check_connection,
        )
        # The pool runs background threads that cannot be joined once the
        # interpreter starts finalising, so close it explicitly on the way out.
        atexit.register(_close_pool)
    return _pool.connection()


def _close_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None


class Cursor:
    """Thin wrapper so one set of SQL works on both backends.

    Placeholders are written as ? and rewritten for Postgres, and rows come
    back as dicts either way.
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        if USE_POSTGRES:
            # psycopg reads % as the start of a placeholder, so a literal one —
            # a LIKE pattern, say — has to be doubled. Escape first, then
            # introduce the real placeholders: doing it the other way round
            # would mangle the %s just written. Without this, SQL containing
            # LIKE works on SQLite and fails only on Postgres, i.e. only in
            # production.
            sql = sql.replace("%", "%%").replace("?", "%s")
        self._raw.execute(sql, tuple(params))
        return self

    def fetchone(self):
        row = self._raw.fetchone()
        return self._to_dict(row)

    def fetchall(self):
        return [self._to_dict(r) for r in self._raw.fetchall()]

    def _to_dict(self, row):
        if row is None:
            return None
        if USE_POSTGRES:
            cols = [d[0] for d in self._raw.description]
            return dict(zip(cols, row))
        return dict(row)


class connection:
    """`with connection() as c:` — commits on success, rolls back on error."""

    def __enter__(self):
        self._ctx = _connect()
        if USE_POSTGRES:
            self._conn = self._ctx.__enter__()
        else:
            self._conn = self._ctx
        self._cur = self._conn.cursor()
        return Cursor(self._cur)

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            if USE_POSTGRES:
                self._ctx.__exit__(exc_type, exc, tb)
            else:
                self._conn.close()
        return False


def _now():
    return datetime.now(timezone.utc).isoformat()


def _pk():
    """Auto-incrementing primary key, spelled for whichever backend is in use."""
    return "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _blob():
    return "BYTEA" if USE_POSTGRES else "BLOB"


def _insert_returning_id(cur, sql, params):
    if USE_POSTGRES:
        cur.execute(sql + " RETURNING id", params)
        return cur.fetchone()["id"]
    cur.execute(sql, params)
    return cur._raw.lastrowid


def init_db():
    with connection() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id {_pk()},
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        _add_column_if_missing(cur, "users", "email_verified", "INTEGER NOT NULL DEFAULT 0")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS tenants (
                id {_pk()},
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                slug TEXT NOT NULL UNIQUE,
                company_name TEXT NOT NULL,
                company_tagline TEXT NOT NULL DEFAULT '',
                brand_color TEXT NOT NULL DEFAULT '#4f46e5',
                support_phone TEXT NOT NULL DEFAULT '',
                support_email TEXT NOT NULL DEFAULT '',
                logo_bytes {_blob()},
                logo_type TEXT NOT NULL DEFAULT '',
                onboarded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS documents (
                id {_pk()},
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, filename)
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS unanswered (
                id {_pk()},
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                question TEXT NOT NULL,
                question_key TEXT NOT NULL,
                times_asked INTEGER NOT NULL DEFAULT 1,
                first_asked TEXT NOT NULL,
                last_asked TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                UNIQUE (tenant_id, question_key)
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS tokens (
                id {_pk()},
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token TEXT NOT NULL UNIQUE,
                purpose TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id {_pk()},
                key TEXT NOT NULL UNIQUE,
                fail_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_docs_tenant ON documents(tenant_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_unanswered_tenant ON unanswered(tenant_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id)")


def _add_column_if_missing(cur, table, column, definition):
    """Add a column to a table created before it existed, on either backend."""
    if USE_POSTGRES:
        existing = {r["column_name"] for r in cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            (table,),
        ).fetchall()}
    else:
        existing = {r["name"] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ---------------------------------------------------------------- users


def create_user(email, password_hash):
    try:
        with connection() as cur:
            return _insert_returning_id(
                cur,
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email.strip().lower(), password_hash, _now()),
            )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise ValueError("An account with that email already exists.")
        raise


def _is_unique_violation(exc):
    text = f"{type(exc).__name__} {exc}".lower()
    return "unique" in text or "duplicate key" in text


def get_user_by_email(email):
    with connection() as cur:
        return cur.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()


def get_user(user_id):
    with connection() as cur:
        return cur.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def update_user_email(user_id, email):
    try:
        with connection() as cur:
            cur.execute("UPDATE users SET email = ? WHERE id = ?",
                        (email.strip().lower(), user_id))
    except Exception as exc:
        if _is_unique_violation(exc):
            raise ValueError("That email is already in use.")
        raise


def update_user_password(user_id, password_hash):
    with connection() as cur:
        cur.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                    (password_hash, user_id))


def set_email_verified(user_id):
    with connection() as cur:
        cur.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))


# --------------------------------------------------------------- tokens
#
# One table serves both password reset and email verification links —
# same shape, different "purpose" and expiry. Kept generic since a future
# use (e.g. invite links) would need nothing more than a new purpose string.

import secrets as _secrets
from datetime import timedelta as _timedelta

TOKEN_TTL_MINUTES = {"reset": 30, "verify": 60 * 24}
RESEND_COOLDOWN_SECONDS = 60


def create_token(user_id, purpose):
    """Issue a fresh token, invalidating any earlier unused one of the same
    purpose so a user can't accumulate a pile of live reset links."""
    now = datetime.now(timezone.utc)
    with connection() as cur:
        cur.execute(
            "DELETE FROM tokens WHERE user_id = ? AND purpose = ? AND used = 0",
            (user_id, purpose),
        )
        token = _secrets.token_urlsafe(32)
        expires_at = (now + _timedelta(minutes=TOKEN_TTL_MINUTES[purpose])).isoformat()
        cur.execute(
            "INSERT INTO tokens (user_id, token, purpose, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, token, purpose, expires_at, now.isoformat()),
        )
    return token


def recently_sent(user_id, purpose):
    """True if a token of this purpose was issued in the last minute.

    A resend button that fires a fresh email on every click is an easy way to
    get an account flagged as spam by a mail provider — this is the guard
    against that, not a security control.
    """
    with connection() as cur:
        row = cur.execute(
            "SELECT created_at FROM tokens WHERE user_id = ? AND purpose = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, purpose),
        ).fetchone()
    if not row:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])
    return age.total_seconds() < RESEND_COOLDOWN_SECONDS


# --------------------------------------------------------- login throttle
#
# Progressive delay, not lockout. An account is never fully inaccessible —
# only slower — which matters because a full lockout is itself a weapon: on
# a public signup app, anyone can guess wrong on purpose to lock out a real
# user. Tracked per-account (key="email:...") AND per-source (key="ip:..."),
# so switching source IP doesn't reset the throttle on one account, and
# spraying guesses across many accounts from one IP still gets slowed down.

LOGIN_BACKOFF_WINDOW_MINUTES = 15
LOGIN_BACKOFF_CAP_SECONDS = 20


def _backoff_seconds(fail_count):
    if fail_count <= 3:
        return 0
    return min(2 ** (fail_count - 3), LOGIN_BACKOFF_CAP_SECONDS)


def get_login_delay(key):
    """Seconds to make this key wait before even checking a password."""
    with connection() as cur:
        row = cur.execute(
            "SELECT fail_count, last_attempt_at FROM login_attempts WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return 0
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["last_attempt_at"])
    if age.total_seconds() > LOGIN_BACKOFF_WINDOW_MINUTES * 60:
        return 0  # stale — treat as a fresh start
    return _backoff_seconds(row["fail_count"])


def record_login_failure(key):
    now = datetime.now(timezone.utc)
    with connection() as cur:
        row = cur.execute(
            "SELECT fail_count, last_attempt_at FROM login_attempts WHERE key = ?", (key,)
        ).fetchone()
        if row:
            age = now - datetime.fromisoformat(row["last_attempt_at"])
            stale = age.total_seconds() > LOGIN_BACKOFF_WINDOW_MINUTES * 60
            new_count = 1 if stale else row["fail_count"] + 1
            cur.execute(
                "UPDATE login_attempts SET fail_count = ?, last_attempt_at = ? WHERE key = ?",
                (new_count, now.isoformat(), key),
            )
        else:
            cur.execute(
                "INSERT INTO login_attempts (key, fail_count, last_attempt_at) VALUES (?, 1, ?)",
                (key, now.isoformat()),
            )


def clear_login_failures(key):
    """Called on a successful login — a correct password is proof this
    account/source isn't the thing that needed throttling."""
    with connection() as cur:
        cur.execute("DELETE FROM login_attempts WHERE key = ?", (key,))


def token_valid(token, purpose):
    """Check without consuming — lets a reset-password page reject a dead
    link immediately on GET, before the user bothers typing a new password."""
    with connection() as cur:
        row = cur.execute(
            "SELECT purpose, expires_at, used FROM tokens WHERE token = ?", (token,)
        ).fetchone()
    if not row or row["purpose"] != purpose or row["used"]:
        return False
    return datetime.fromisoformat(row["expires_at"]) >= datetime.now(timezone.utc)


def consume_token(token, purpose):
    """Validate and burn a token in one step. Returns the user_id, or None if
    the token is missing, wrong purpose, already used, or expired."""
    with connection() as cur:
        row = cur.execute(
            "SELECT id, user_id, purpose, expires_at, used FROM tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row or row["purpose"] != purpose or row["used"]:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            return None
        cur.execute("UPDATE tokens SET used = 1 WHERE id = ?", (row["id"],))
        return row["user_id"]


# -------------------------------------------------------------- tenants


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:48] or "workspace"


def unique_slug(name):
    """Append -2, -3 … until the slug is free and not reserved."""
    base = slugify(name)
    candidate, n = base, 1
    with connection() as cur:
        while True:
            if candidate not in RESERVED_SLUGS and len(candidate) >= 3:
                taken = cur.execute(
                    "SELECT 1 FROM tenants WHERE slug = ?", (candidate,)
                ).fetchone()
                if not taken:
                    return candidate
            n += 1
            candidate = f"{base}-{n}"


def create_tenant(user_id, company_name, slug):
    try:
        with connection() as cur:
            return _insert_returning_id(
                cur,
                "INSERT INTO tenants (user_id, slug, company_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, slug, company_name.strip(), _now()),
            )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise ValueError("That workspace address is already taken.")
        raise


def get_tenant_by_slug(slug):
    if not SLUG_RE.match(slug or ""):
        return None
    with connection() as cur:
        return cur.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()


def get_tenant_for_user(user_id):
    with connection() as cur:
        return cur.execute(
            "SELECT * FROM tenants WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,)
        ).fetchone()


def update_tenant(tenant_id, **fields):
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    with connection() as cur:
        cur.execute(f"UPDATE tenants SET {columns} WHERE id = ?",
                    [*fields.values(), tenant_id])


def support_contact_line(tenant):
    """The contact the bot offers when it can't answer."""
    parts = [p for p in (tenant.get("support_phone"), tenant.get("support_email")) if p]
    return " or ".join(parts)


# ------------------------------------------------------------ documents


def save_document(tenant_id, filename, content):
    """Insert or replace one document. Text lives in the database so it
    survives redeploys — the container's disk does not."""
    now = _now()
    with connection() as cur:
        cur.execute(
            """
            INSERT INTO documents (tenant_id, filename, content, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (tenant_id, filename) DO UPDATE SET
                content = excluded.content,
                updated_at = excluded.updated_at
            """,
            (tenant_id, filename, content, now),
        )


def get_documents(tenant_id):
    with connection() as cur:
        return cur.execute(
            "SELECT filename, content FROM documents WHERE tenant_id = ? ORDER BY filename",
            (tenant_id,),
        ).fetchall()


def list_document_names(tenant_id):
    return [d["filename"] for d in get_documents(tenant_id)]


def delete_document(tenant_id, filename):
    with connection() as cur:
        cur.execute("DELETE FROM documents WHERE tenant_id = ? AND filename = ?",
                    (tenant_id, filename))


# ---------------------------------------------------------------- logos


def save_logo(tenant_id, data, content_type):
    with connection() as cur:
        cur.execute("UPDATE tenants SET logo_bytes = ?, logo_type = ? WHERE id = ?",
                    (data, content_type, tenant_id))


def clear_logo(tenant_id):
    with connection() as cur:
        cur.execute("UPDATE tenants SET logo_bytes = NULL, logo_type = '' WHERE id = ?",
                    (tenant_id,))


def get_logo(slug):
    with connection() as cur:
        row = cur.execute(
            "SELECT logo_bytes, logo_type FROM tenants WHERE slug = ?", (slug,)
        ).fetchone()
    if not row or not row["logo_bytes"]:
        return None, None
    return bytes(row["logo_bytes"]), row["logo_type"]


# --------------------------------------------------------- unanswered


def _question_key(question):
    """Collapse near-identical phrasings so the same gap counts as one row."""
    return " ".join(re.sub(r"[^\w\s]", "", question.lower()).split())[:200]


def record_unanswered(tenant_id, question):
    question = question.strip()
    if not question:
        return
    now = _now()
    with connection() as cur:
        cur.execute(
            """
            INSERT INTO unanswered (tenant_id, question, question_key, first_asked, last_asked)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, question_key) DO UPDATE SET
                times_asked = unanswered.times_asked + 1,
                last_asked = excluded.last_asked,
                resolved = 0
            """,
            (tenant_id, question, _question_key(question), now, now),
        )


def get_unanswered(tenant_id, include_resolved=False):
    sql = ("SELECT id, question, times_asked, first_asked, last_asked, resolved "
           "FROM unanswered WHERE tenant_id = ?")
    if not include_resolved:
        sql += " AND resolved = 0"
    sql += " ORDER BY times_asked DESC, last_asked DESC LIMIT 100"
    with connection() as cur:
        return cur.execute(sql, (tenant_id,)).fetchall()


def resolve_unanswered(tenant_id, row_id):
    """Scoped by tenant so one owner can't touch another's list."""
    with connection() as cur:
        cur.execute("UPDATE unanswered SET resolved = 1 WHERE id = ? AND tenant_id = ?",
                    (row_id, tenant_id))

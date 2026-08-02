"""SQLite storage for the multi-tenant app.

Three tables:
  users        — people who sign up (email + hashed password)
  tenants      — one business workspace per user (branding + slug)
  chat_history — every Q&A, scoped to the tenant it belongs to

Every tenant-scoped query takes a tenant_id so one business can never read
another's data.
"""

import re
import sqlite3
from datetime import datetime, timezone

DB_PATH = "chatbot.db"

# Slugs end up in URLs and on the filesystem, so keep them strictly safe.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
RESERVED_SLUGS = {
    "admin", "api", "login", "signup", "logout", "dashboard", "static",
    "settings", "profile", "documents", "c", "help", "about", "new",
}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            slug TEXT NOT NULL UNIQUE,
            company_name TEXT NOT NULL,
            company_tagline TEXT NOT NULL DEFAULT '',
            logo_emoji TEXT NOT NULL DEFAULT '💬',
            logo_path TEXT NOT NULL DEFAULT '',
            brand_color TEXT NOT NULL DEFAULT '#2563eb',
            support_contact TEXT NOT NULL DEFAULT '',
            onboarded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            sources TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_tenant ON chat_history(tenant_id)")

    # migrations for databases created before a column existed
    _add_column_if_missing(conn, "tenants", "logo_path", "TEXT NOT NULL DEFAULT ''")

    conn.commit()
    conn.close()


def _add_column_if_missing(conn, table, column, definition):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ---------------------------------------------------------------- users


def create_user(email, password_hash):
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email.strip().lower(), password_hash, _now()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("An account with that email already exists.")
    finally:
        conn.close()


def get_user_by_email(email):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(user_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_user_email(user_id, email):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET email = ? WHERE id = ?", (email.strip().lower(), user_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError("That email is already in use.")
    finally:
        conn.close()


def update_user_password(user_id, password_hash):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
    )
    conn.commit()
    conn.close()


# -------------------------------------------------------------- tenants


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:48] or "workspace"


def unique_slug(name):
    """Append -2, -3 … until the slug is free and not reserved."""
    base = slugify(name)
    conn = get_connection()
    candidate, n = base, 1
    while True:
        if candidate not in RESERVED_SLUGS and len(candidate) >= 3:
            taken = conn.execute(
                "SELECT 1 FROM tenants WHERE slug = ?", (candidate,)
            ).fetchone()
            if not taken:
                break
        n += 1
        candidate = f"{base}-{n}"
    conn.close()
    return candidate


def create_tenant(user_id, company_name, slug):
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO tenants (user_id, slug, company_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, slug, company_name.strip(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("That workspace address is already taken.")
    finally:
        conn.close()


def get_tenant_by_slug(slug):
    if not SLUG_RE.match(slug or ""):
        return None
    conn = get_connection()
    row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_tenant_for_user(user_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM tenants WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_tenant(tenant_id, **fields):
    if not fields:
        return
    conn = get_connection()
    columns = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE tenants SET {columns} WHERE id = ?", [*fields.values(), tenant_id]
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------- chat history


def save_message(tenant_id, question, answer, sources):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO chat_history (tenant_id, question, answer, sources, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (tenant_id, question, answer, ", ".join(sources), _now()),
    )
    conn.commit()
    conn.close()


def get_history(tenant_id, limit=50):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT question, answer, sources, created_at FROM chat_history
        WHERE tenant_id = ? ORDER BY id DESC LIMIT ?
        """,
        (tenant_id, limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

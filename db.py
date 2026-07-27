"""SQLite chat history — one table, no ORM needed for a project this size."""

import sqlite3
from datetime import datetime

DB_PATH = "chatbot.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            sources TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def save_message(question, answer, sources):
    conn = get_connection()
    conn.execute(
        "INSERT INTO chat_history (question, answer, sources, created_at) VALUES (?, ?, ?, ?)",
        (question, answer, ", ".join(sources), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_history(limit=50):
    conn = get_connection()
    rows = conn.execute(
        "SELECT question, answer, sources, created_at FROM chat_history ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

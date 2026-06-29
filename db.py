"""
db.py — SQLite helpers for job-hunter-portal.
"""

import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def _random_code() -> str:
    part = lambda: "".join(secrets.choice(_CODE_CHARS) for _ in range(4))
    return f"{part()}-{part()}"

DB_PATH = Path(__file__).parent / "portal.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL,
                email            TEXT    NOT NULL UNIQUE,
                location         TEXT    NOT NULL,
                password_hash    TEXT,
                invite_code_used TEXT,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                active           INTEGER  DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS queries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id),
                query_string TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invite_codes (
                code                TEXT PRIMARY KEY,
                created_by_user_id  INTEGER REFERENCES users(id),
                used_by_user_id     INTEGER REFERENCES users(id),
                used_at             DATETIME
            );

            CREATE TABLE IF NOT EXISTS seen_jobs (
                user_id  INTEGER NOT NULL REFERENCES users(id),
                job_id   TEXT    NOT NULL,
                seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, job_id)
            );
        """)
        # migrate existing DB — add columns if not present
        for col_sql in [
            "ALTER TABLE users ADD COLUMN password_hash TEXT",
            "ALTER TABLE users ADD COLUMN delivery_days TEXT NOT NULL DEFAULT '1111100'",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass


# ── Users ─────────────────────────────────────────────────────────────────────

def get_all_active_users():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE active = 1").fetchall()


def get_user_by_id(user_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_all_users():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()


def get_user_by_email(email: str):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def create_user(name: str, email: str, location: str, invite_code_used: str, password_hash: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (name, email, location, invite_code_used, password_hash) VALUES (?, ?, ?, ?, ?)",
            (name, email, location, invite_code_used, password_hash),
        )
        return cur.lastrowid


def set_delivery_days(user_id: int, bitmask: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET delivery_days = ? WHERE id = ?", (bitmask, user_id))


def get_users_for_today() -> list:
    """Return active users whose delivery_days bitmask includes today (0=Mon, 6=Sun)."""
    today_idx = datetime.now().weekday()
    with get_conn() as conn:
        users = conn.execute("SELECT * FROM users WHERE active = 1").fetchall()
        return [
            u for u in users
            if (u["delivery_days"] or "0000000")[today_idx] == "1"
        ]


# ── Seen jobs (per-user deduplication) ───────────────────────────────────────

def is_seen_for_user(user_id: int, job_id: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM seen_jobs WHERE user_id = ? AND job_id = ?", (user_id, job_id)
        ).fetchone() is not None


def mark_seen_for_user(user_id: int, job_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_jobs (user_id, job_id) VALUES (?, ?)", (user_id, job_id)
        )


# ── Queries ───────────────────────────────────────────────────────────────────

def get_queries_for_user(user_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT query_string FROM queries WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [r["query_string"] for r in rows]


def set_queries_for_user(user_id: int, query_strings: list):
    """Replace all queries for a user (max 8)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM queries WHERE user_id = ?", (user_id,))
        for q in query_strings[:8]:
            q = q.strip()
            if q:
                conn.execute(
                    "INSERT INTO queries (user_id, query_string) VALUES (?, ?)",
                    (user_id, q),
                )


# ── Invite codes ──────────────────────────────────────────────────────────────

def get_invite_code(code: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM invite_codes WHERE code = ?", (code,)
        ).fetchone()


def create_invite_code(code: str, created_by_user_id: int = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO invite_codes (code, created_by_user_id) VALUES (?, ?)",
            (code, created_by_user_id),
        )


def use_invite_code(code: str, user_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE invite_codes SET used_by_user_id = ?, used_at = CURRENT_TIMESTAMP WHERE code = ?",
            (user_id, code),
        )


def generate_codes(n: int, created_by_user_id: int = None) -> list:
    """Generate n unique invite codes and insert them. Returns the code strings."""
    codes = []
    with get_conn() as conn:
        while len(codes) < n:
            code = _random_code()
            exists = conn.execute(
                "SELECT 1 FROM invite_codes WHERE code = ?", (code,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO invite_codes (code, created_by_user_id) VALUES (?, ?)",
                    (code, created_by_user_id),
                )
                codes.append(code)
    return codes


def get_codes_for_user(user_id: int) -> list:
    """Return outbound codes generated for this user to share (unused only)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code, used_by_user_id FROM invite_codes WHERE created_by_user_id = ?",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_codes() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ic.code, ic.used_at,
                   u1.name AS created_by, u2.name AS used_by
            FROM invite_codes ic
            LEFT JOIN users u1 ON ic.created_by_user_id = u1.id
            LEFT JOIN users u2 ON ic.used_by_user_id  = u2.id
            ORDER BY ic.rowid DESC
        """).fetchall()
        return [dict(r) for r in rows]

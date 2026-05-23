"""
database.py — SQLite persistence layer
Tables:
  users        — Telegram users, credit balance, free-credit flag
  recharges    — Pending / approved recharge requests
  runs         — Offer run history
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/data/swiggy_bot.db")   # mounted volume on Railway
_local = threading.local()


def _conn() -> sqlite3.Connection:
    """Return a per-thread connection (creates file & tables on first use)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _create_tables(conn)
    return _local.conn


def _create_tables(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        username    TEXT,
        full_name   TEXT,
        credits     INTEGER NOT NULL DEFAULT 0,
        free_given  INTEGER NOT NULL DEFAULT 0,   -- 1 = already got free credits
        joined_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        last_seen   TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS recharges (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        username    TEXT,
        utr         TEXT    NOT NULL,
        amount      TEXT    NOT NULL DEFAULT '₹20',
        credits_req INTEGER NOT NULL DEFAULT 40,
        status      TEXT    NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        resolved_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL,
        credits_used INTEGER NOT NULL DEFAULT 2,
        status       TEXT    NOT NULL DEFAULT 'started',  -- started | done | failed
        success_cnt  INTEGER DEFAULT 0,
        failed_cnt   INTEGER DEFAULT 0,
        total_earned TEXT,
        started_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        finished_at  TEXT,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    );
    """)
    conn.commit()


# ── User helpers ───────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str | None, full_name: str) -> sqlite3.Row:
    conn = _conn()
    conn.execute("""
        INSERT INTO users(user_id, username, full_name)
        VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username  = excluded.username,
            full_name = excluded.full_name,
            last_seen = datetime('now')
    """, (user_id, username or "", full_name))
    conn.commit()
    return get_user(user_id)


def get_user(user_id: int) -> sqlite3.Row | None:
    return _conn().execute(
        "SELECT * FROM users WHERE user_id=?", (user_id,)
    ).fetchone()


def give_free_credits(user_id: int, amount: int = 2) -> bool:
    """Give free credits once. Returns True if granted, False if already given."""
    conn = _conn()
    row = conn.execute(
        "SELECT free_given FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    if row is None or row["free_given"]:
        return False
    conn.execute(
        "UPDATE users SET credits=credits+?, free_given=1 WHERE user_id=?",
        (amount, user_id),
    )
    conn.commit()
    return True


def get_credits(user_id: int) -> int:
    row = get_user(user_id)
    return row["credits"] if row else 0


def deduct_credits(user_id: int, amount: int = 2) -> bool:
    """Deduct credits. Returns False if insufficient."""
    conn = _conn()
    row = conn.execute(
        "SELECT credits FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row or row["credits"] < amount:
        return False
    conn.execute(
        "UPDATE users SET credits=credits-? WHERE user_id=?",
        (amount, user_id),
    )
    conn.commit()
    return True


def add_credits(user_id: int, amount: int) -> int:
    """Admin: add credits. Returns new balance."""
    conn = _conn()
    conn.execute(
        "UPDATE users SET credits=credits+? WHERE user_id=?",
        (amount, user_id),
    )
    conn.commit()
    row = get_user(user_id)
    return row["credits"] if row else 0


# ── Recharge helpers ───────────────────────────────────────────────────────────

def create_recharge(user_id: int, username: str | None, utr: str,
                    credits_req: int = 40) -> int:
    conn = _conn()
    cur = conn.execute("""
        INSERT INTO recharges(user_id, username, utr, credits_req)
        VALUES (?,?,?,?)
    """, (user_id, username or "", utr, credits_req))
    conn.commit()
    return cur.lastrowid


def get_pending_recharges():
    return _conn().execute(
        "SELECT * FROM recharges WHERE status='pending' ORDER BY created_at"
    ).fetchall()


def resolve_recharge(recharge_id: int, action: str) -> sqlite3.Row | None:
    """action = 'approved' | 'rejected'. Returns the recharge row."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM recharges WHERE id=?", (recharge_id,)
    ).fetchone()
    if not row:
        return None
    conn.execute("""
        UPDATE recharges
        SET status=?, resolved_at=datetime('now')
        WHERE id=?
    """, (action, recharge_id))
    if action == "approved":
        conn.execute(
            "UPDATE users SET credits=credits+? WHERE user_id=?",
            (row["credits_req"], row["user_id"]),
        )
    conn.commit()
    return row


def get_recharge(recharge_id: int) -> sqlite3.Row | None:
    return _conn().execute(
        "SELECT * FROM recharges WHERE id=?", (recharge_id,)
    ).fetchone()


# ── Run helpers ────────────────────────────────────────────────────────────────

def start_run(user_id: int) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO runs(user_id) VALUES (?)", (user_id,)
    )
    conn.commit()
    return cur.lastrowid


def finish_run(run_id: int, status: str, success: int, failed: int, earned: str):
    conn = _conn()
    conn.execute("""
        UPDATE runs SET status=?, success_cnt=?, failed_cnt=?,
                        total_earned=?, finished_at=datetime('now')
        WHERE id=?
    """, (status, success, failed, earned, run_id))
    conn.commit()


def user_run_count(user_id: int) -> int:
    row = _conn().execute(
        "SELECT COUNT(*) AS c FROM runs WHERE user_id=? AND status='done'",
        (user_id,),
    ).fetchone()
    return row["c"] if row else 0

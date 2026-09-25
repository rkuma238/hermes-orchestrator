"""SQLite-backed accounts / API keys / invocation log for the reference registry.

Not a production auth store (no key rotation, no rate limiting) — it exists
to make the Envoy ext_authz flow and the publisher dashboard real and
testable rather than mocked.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

# Overridable so tests (and any second registry instance) don't share state
# with a dev instance's accounts db.
DB_PATH = Path(os.environ.get("OSP_REGISTRY_DB", str(Path(__file__).parent / "data" / "registry.db")))


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            key_hash TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id TEXT NOT NULL,
            skill_version TEXT NOT NULL,
            owner_account_id TEXT,
            caller_account_id TEXT,
            success INTEGER NOT NULL,
            error TEXT,
            ts REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def create_account(name: str) -> tuple[str, str]:
    """Returns (account_id, api_key). The plaintext api_key is only ever
    available here, at creation time — only its hash is stored."""
    account_id = "acct_" + secrets.token_hex(8)
    api_key = "osp_" + secrets.token_urlsafe(32)
    conn = _conn()
    now = time.time()
    conn.execute("INSERT INTO accounts (id, name, created_at) VALUES (?, ?, ?)", (account_id, name, now))
    conn.execute(
        "INSERT INTO api_keys (key_hash, account_id, created_at) VALUES (?, ?, ?)",
        (_hash_key(api_key), account_id, now),
    )
    conn.commit()
    conn.close()
    return account_id, api_key


def resolve_api_key(api_key: str) -> str | None:
    conn = _conn()
    row = conn.execute(
        "SELECT account_id FROM api_keys WHERE key_hash = ?", (_hash_key(api_key),)
    ).fetchone()
    conn.close()
    return row["account_id"] if row else None


def account_exists(account_id: str) -> bool:
    conn = _conn()
    row = conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone()
    conn.close()
    return row is not None


def record_invocation(
    *,
    skill_id: str,
    skill_version: str,
    owner_account_id: str | None,
    caller_account_id: str | None,
    success: bool,
    error: str | None,
) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO invocations
           (skill_id, skill_version, owner_account_id, caller_account_id, success, error, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (skill_id, skill_version, owner_account_id, caller_account_id, int(success), error, time.time()),
    )
    conn.commit()
    conn.close()


def list_invocations_for_owner(owner_account_id: str, limit: int = 200) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        """SELECT * FROM invocations WHERE owner_account_id = ?
           ORDER BY ts DESC LIMIT ?""",
        (owner_account_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

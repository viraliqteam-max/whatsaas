# -*- coding: utf-8 -*-
"""
config.py — App-wide configuration.

Groq client, database path, logging, and outbound message helper.
All other modules import from here.
"""

import os
import sqlite3
import threading
from datetime import datetime

import groq as _groq_module

# ── Shared lock (guards _conv_locks / _conv_timers in agent.py) ──────────────
_meta_lock = threading.Lock()


# ── Groq configuration ────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL_NAME   = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
TEMPERATURE  = float(os.environ.get("GROQ_TEMPERATURE", "0.7"))


# ── Groq client (lazy singleton) ──────────────────────────────────────────────
_groq_client      = None
_groq_client_lock = threading.Lock()


def _get_groq_client() -> _groq_module.Groq:
    """Return a cached Groq client, creating it on first call."""
    global _groq_client
    with _groq_client_lock:
        if _groq_client is None:
            if not GROQ_API_KEY:
                raise RuntimeError(
                    "GROQ_API_KEY is not set. "
                    "Export it as an environment variable before starting the app."
                )
            _groq_client = _groq_module.Groq(api_key=GROQ_API_KEY)
    return _groq_client


def groq_chat(
    messages: list,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    timeout: int = 30,
):
    """
    Call the Groq chat completions endpoint.

    Uses MODULE-LEVEL defaults (MODEL_NAME, TEMPERATURE) unless overridden.
    Raises the raw Groq exception on failure — callers decide how to handle.
    """
    client = _get_groq_client()
    kwargs: dict = {
        "model":       MODEL_NAME,
        "messages":    messages,
        "temperature": temperature if temperature is not None else TEMPERATURE,
        "timeout":     timeout,
    }
    if top_p is not None:
        kwargs["top_p"] = top_p
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    return client.chat.completions.create(**kwargs)


# ── Database ──────────────────────────────────────────────────────────────────
DB = "wati_chat.db"


# ── In-memory log (last 50 entries, shown in dashboard) ──────────────────────
send_log: list[str] = []


def _log(msg: str) -> None:
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(entry)
    send_log.append(entry)
    if len(send_log) > 50:
        send_log.pop(0)


# ── Outbound message stub ─────────────────────────────────────────────────────
WEBHOOK_URL = ""   # set by pyngrok at startup in app.py


def send_message(to_number: str, message: str) -> None:
    """Log outbound message locally (replace with real delivery in production)."""
    _log(f"LOCAL ONLY — outbound saved for {to_number}: {message[:80]}")


# ── Database init + migration ─────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id                   TEXT    PRIMARY KEY,
                phone_number         TEXT    UNIQUE NOT NULL,
                name                 TEXT    DEFAULT '',
                occupation           TEXT    DEFAULT '',
                interest_level       TEXT    DEFAULT 'New',
                interest_score       INTEGER DEFAULT 0,
                interest_signals     TEXT    DEFAULT '[]',
                recommendation       TEXT    DEFAULT 'Just started — wait for customer reply.',
                created_at           TEXT,
                updated_at           TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                role            TEXT,
                content         TEXT,
                timestamp       TEXT,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Migrate existing DB — silently add missing columns
        for col, typ, default in [
            ("name",                  "TEXT",    "''"),
            ("occupation",            "TEXT",    "''"),
            ("chat_mode",             "TEXT",    "'auto'"),
            ("last_processed_msg_id", "INTEGER", "0"),
            ("language_lock",         "TEXT",    "''"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE conversations ADD COLUMN {col} {typ} DEFAULT {default}"
                )
            except Exception:
                pass   # column already exists
        conn.commit()


def load_settings_from_db() -> None:
    """
    Optionally override GROQ_API_KEY and ngrok token from the settings table.
    Called at startup after init_db().
    """
    global GROQ_API_KEY, _groq_client

    try:
        with sqlite3.connect(DB) as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        cfg = {r[0]: r[1] for r in rows}
    except Exception:
        return

    # Reload Groq client if a key is stored in the DB
    saved_key = cfg.get("groq_api_key", "") or GROQ_API_KEY
    if saved_key and saved_key != GROQ_API_KEY:
        GROQ_API_KEY = saved_key
        with _groq_client_lock:
            _groq_client = None   # force re-creation with new key
        _log(f"[Groq] API key reloaded from DB — key: {GROQ_API_KEY[:12]}...")

    ngrok_token = cfg.get("ngrok_token", "")
    if ngrok_token:
        try:
            from pyngrok import conf as _nc
            _nc.get_default().auth_token = ngrok_token
        except Exception:
            pass

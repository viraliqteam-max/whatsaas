# -*- coding: utf-8 -*-
"""
agent.py — AI Sales Agent & Campaign Engine.

Separates high-priority incoming replies from low-priority bulk campaigns.
Uses Groq for intelligent responses and a queue-based system for controlled sending.
"""

import sqlite3
import json
import time
import random
import threading
import re
import queue
from datetime import datetime, timedelta

from config import DB, groq_chat, send_message, _log, _meta_lock
from prompts import ANALYSIS_PROMPT, SYSTEM_PROMPT, build_system_prompt


# ── Constants & State ────────────────────────────────────────────────────────
CAMPAIGN_QUEUE = queue.Queue()
PROCESS_LOCKS = {}  # contact_id -> threading.Lock
PENDING_REPLIES = {}  # contact_id -> threading.Timer

# Delay settings
REPLY_DELAY_MIN = 120  # 2 minutes
REPLY_DELAY_MAX = 180  # 3 minutes
CAMPAIGN_DELAY_MIN = 120
CAMPAIGN_DELAY_MAX = 180


# ── Database Helpers ─────────────────────────────────────────────────────────

def _db_query(query, params=(), fetch=False):
    with sqlite3.connect(DB) as conn:
        cursor = conn.execute(query, params)
        if fetch:
            return cursor.fetchall()
        conn.commit()
        return cursor.lastrowid


def _get_contact_lock(contact_id):
    with _meta_lock:
        if contact_id not in PROCESS_LOCKS:
            PROCESS_LOCKS[contact_id] = threading.Lock()
        return PROCESS_LOCKS[contact_id]


# ── Deduplication & Processing ───────────────────────────────────────────────

def is_message_processed(contact_id, preview):
    """Check if this specific message preview was already processed."""
    rows = _db_query(
        "SELECT id FROM messages WHERE conversation_id=? AND content=? AND role='user' LIMIT 1",
        (contact_id, preview),
        fetch=True
    )
    return len(rows) > 0


def mark_processed(contact_id, role, content):
    """Save message to DB to prevent duplicate processing."""
    ts = datetime.now().strftime("%I:%M %p")
    _db_query(
        "INSERT INTO messages (conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (contact_id, role, content, ts)
    )


# ── LLM Core ────────────────────────────────────────────────────────────────

def generate_reply(user_message: str, chat_history: list = None, occupation: str = "") -> str:
    """Generates a clean sales reply using Groq."""
    chat_history = chat_history or []
    system_prompt = build_system_prompt(occupation)

    # Optional Analysis Pass
    try:
        analysis_resp = groq_chat(
            messages=[
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": f"History: {chat_history}\nLatest: {user_message}"}
            ],
            temperature=0.1,
            max_tokens=150
        )
        analysis = (analysis_resp.choices[0].message.content or "").strip()
    except Exception as e:
        _log(f"Analysis failed: {e}")
        analysis = ""

    # Final Reply Pass
    messages = [{"role": "system", "content": f"{system_prompt}\n\nAnalysis:\n{analysis}"}]
    for turn in chat_history[-10:]:
        messages.append({"role": turn['role'], "content": turn['content']})
    messages.append({"role": "user", "content": user_message})

    try:
        resp = groq_chat(messages=messages, temperature=0.7, max_tokens=80)
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        _log(f"Groq generation failed: {e}")
        return ""


# ── FLOW A: Reply Flow (High Priority) ───────────────────────────────────────

def execute_delayed_reply(contact_id, phone_number, last_message):
    """Actual execution of the reply after the wait period."""
    lock = _get_contact_lock(contact_id)
    if not lock.acquire(blocking=False):
        return

    try:
        # Load context
        conv = _db_query("SELECT occupation FROM conversations WHERE id=?", (contact_id,), fetch=True)
        occupation = conv[0][0] if conv else ""
        
        raw_history = _db_query(
            "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 10",
            (contact_id,), fetch=True
        )
        history = [{"role": r[0], "content": r[1]} for r in reversed(raw_history)]

        # Generate
        _log(f"Generating reply for {phone_number}...")
        reply = generate_reply(last_message, history, occupation)
        
        if reply:
            send_message(phone_number, reply)
            mark_processed(contact_id, 'assistant', reply)
            _log(f"Reply sent to {phone_number}")
    except Exception as e:
        _log(f"Error in delayed reply: {e}")
    finally:
        lock.release()
        with _meta_lock:
            PENDING_REPLIES.pop(contact_id, None)


def on_incoming_message(phone_number, message_content):
    """Triggered when a new unread message is detected."""
    # 1. Ensure conversation exists
    rows = _db_query("SELECT id FROM conversations WHERE phone_number=?", (phone_number,), fetch=True)
    if not rows:
        contact_id = str(_db_query(
            "INSERT INTO conversations (id, phone_number, created_at) VALUES (?, ?, ?)",
            (str(int(time.time())), phone_number, datetime.now().isoformat())
        ))
    else:
        contact_id = rows[0][0]

    # 2. Deduplication
    if is_message_processed(contact_id, message_content):
        return

    mark_processed(contact_id, 'user', message_content)
    _log(f"New message from {phone_number}: {message_content[:50]}...")

    # 3. Schedule delayed reply
    with _meta_lock:
        if contact_id in PENDING_REPLIES:
            PENDING_REPLIES[contact_id].cancel()
        
        delay = random.randint(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
        _log(f"Scheduling reply for {phone_number} in {delay}s")
        
        t = threading.Timer(delay, execute_delayed_reply, args=(contact_id, phone_number, message_content))
        PENDING_REPLIES[contact_id] = t
        t.start()


# ── FLOW B: Campaign Flow (Low Priority) ─────────────────────────────────────

def campaign_worker():
    """Background worker that processes campaign messages one by one with delays."""
    _log("Campaign worker started.")
    while True:
        try:
            # Get next task from queue (blocks until available)
            task = CAMPAIGN_QUEUE.get()
            phone_number = task['phone']
            message = task['message']
            
            _log(f"Processing campaign message for {phone_number}...")
            
            # Check if we should skip (e.g., if user replied recently)
            # This ensures we don't interrupt active conversations with bulk spam
            recent = _db_query(
                "SELECT id FROM messages WHERE conversation_id=(SELECT id FROM conversations WHERE phone_number=?) "
                "AND role='user' AND timestamp > ?",
                (phone_number, (datetime.now() - timedelta(hours=1)).strftime("%I:%M %p")),
                fetch=True
            )
            
            if recent:
                _log(f"Skipping campaign for {phone_number} - active conversation detected.")
            else:
                send_message(phone_number, message)
                _log(f"Campaign message sent to {phone_number}")

            # Mandatory delay between campaign messages to prevent spam detection
            delay = random.randint(CAMPAIGN_DELAY_MIN, CAMPAIGN_DELAY_MAX)
            _log(f"Waiting {delay}s before next campaign message...")
            time.sleep(delay)
            
        except Exception as e:
            _log(f"Campaign worker error: {e}")
            time.sleep(10)
        finally:
            CAMPAIGN_QUEUE.task_done()


def start_campaign(messages):
    """
    Public entry point to start a bulk campaign.
    Expects messages: list of {'phone': str, 'message': str}
    """
    for msg in messages:
        CAMPAIGN_QUEUE.put(msg)
    _log(f"Added {len(messages)} messages to campaign queue.")


# ── Integration & Polling ───────────────────────────────────────────────────

def poll_and_process(driver):
    """
    Reads unread messages from WhatsApp Web via Selenium.
    This should be called periodically by the main application loop.
    """
    from utils.whatsapp_automation import poll_inbox
    
    try:
        unread_chats = poll_inbox(driver)
        for chat in unread_chats:
            name = chat.get('sender_name')
            preview = chat.get('message_preview')
            
            # Use name as phone_number if phone isn't visible in sidebar
            # (Extension/Selenium will resolve it when opening the chat)
            if name:
                on_incoming_message(name, preview)
                
    except Exception as e:
        _log(f"Inbox polling failed: {e}")


# ── Initialization ──────────────────────────────────────────────────────────

# Start campaign worker in a background daemon thread
threading.Thread(target=campaign_worker, daemon=True).start()


"""
Compatibility facade for session service imports.

Incoming message and auto-reply processing now belongs to apps.autoreply. The
session app still owns WhatsAppSession, Conversation, and IncomingMessage
models, so this module remains as a stable import path during the refactor.
"""
from apps.autoreply.services.incoming import (
    DEDUP_WINDOW_MINUTES,
    REPLY_DELAY_MAX,
    REPLY_DELAY_MIN,
    process_incoming_message,
)

__all__ = [
    "DEDUP_WINDOW_MINUTES",
    "REPLY_DELAY_MAX",
    "REPLY_DELAY_MIN",
    "process_incoming_message",
]

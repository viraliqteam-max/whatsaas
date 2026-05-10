import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone


COOLDOWN_SECONDS = 60
PROCESSING_TTL_SECONDS = 90
MESSAGE_SEEN_TTL_SECONDS = 24 * 60 * 60


@dataclass
class SkipDecision:
    should_skip: bool
    reason: str = ""


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode("utf-8")).hexdigest()


def begin_processing(conversation_id: int) -> SkipDecision:
    key = f"ai:processing:{conversation_id}"
    if not cache.add(key, "1", timeout=PROCESSING_TTL_SECONDS):
        return SkipDecision(True, "processing_lock")
    return SkipDecision(False)


def end_processing(conversation_id: int) -> None:
    cache.delete(f"ai:processing:{conversation_id}")


def should_skip(conversation, state, incoming_text: str, external_message_id: str = "") -> SkipDecision:
    if state.human_active:
        return SkipDecision(True, "human_active")
    if state.ai_paused_until and state.ai_paused_until > timezone.now():
        return SkipDecision(True, "ai_paused")

    if external_message_id:
        seen_key = f"wa:message_seen:{external_message_id}"
        if not cache.add(seen_key, "1", timeout=MESSAGE_SEEN_TTL_SECONDS):
            return SkipDecision(True, "duplicate_external_message")

    cooldown_key = f"ai:cooldown:{conversation.id}"
    if cache.get(cooldown_key):
        return SkipDecision(True, "cooldown")

    last_ai = conversation.ai_messages.filter(sender="ai").order_by("-created_at", "-id").first()
    if last_ai and _hash(last_ai.text) == _hash(incoming_text):
        return SkipDecision(True, "echoed_ai_reply")

    recent_ai_count = conversation.ai_messages.filter(
        sender="ai",
        created_at__gte=timezone.now() - timedelta(minutes=5),
    ).count()
    recent_user_count = conversation.ai_messages.filter(
        sender="user",
        created_at__gte=timezone.now() - timedelta(minutes=5),
    ).count()
    if recent_ai_count >= 2 and recent_user_count == 0:
        return SkipDecision(True, "ai_message_loop")

    return SkipDecision(False)


def mark_ai_replied(conversation_id: int, reply_text: str) -> None:
    cache.set(f"ai:cooldown:{conversation_id}", "1", timeout=COOLDOWN_SECONDS)
    cache.set(f"ai:last_reply_hash:{conversation_id}", _hash(reply_text), timeout=MESSAGE_SEEN_TTL_SECONDS)


def is_repeated_reply(conversation, reply_text: str) -> bool:
    reply_hash = _hash(reply_text)
    cached = cache.get(f"ai:last_reply_hash:{conversation.id}")
    if cached and cached == reply_hash:
        return True
    return conversation.ai_messages.filter(sender="ai", text__iexact=(reply_text or "").strip()).exists()

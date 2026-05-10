import logging
import random
import re
import threading
import time
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import cache
from django.utils import timezone

from apps.autoreply.services.message_filters import clean_sender_name, should_ignore_incoming_event
from apps.sessions.models import IncomingMessage, WhatsAppSession
from shared.utils.jid import jid_from_phone, jid_to_phone, normalize_jid, validate_jid

logger = logging.getLogger(__name__)

# ── Reply delay configuration ─────────────────────────────────────────────────
REPLY_DELAY_MIN = 120   # 2 minutes
REPLY_DELAY_MAX = 180   # 3 minutes
DEDUP_WINDOW_MINUTES = 10  # ignore same preview from same sender within this window

# Per-sender in-memory guard: key="session_id:jid_or_name", value=True while pending.
# Prevents double-scheduling when the extension scans the same unread chat multiple times.
_reply_pending: dict = {}
_reply_versions: dict = {}
_reply_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_sender_name(sender_name: str) -> str:
    return clean_sender_name(sender_name)


def _ignore_incoming(sender_name: str, preview: str) -> bool:
    return should_ignore_incoming_event(sender_name, preview)


def _effective_preview(sender_name: str, preview: str) -> str:
    preview = (preview or "").strip()
    preview_digits = re.sub(r"\D", "", preview)
    if preview == sender_name or preview_digits == sender_name:
        return ""
    return preview


def _jid_to_phone(jid: str) -> str:
    """Extract the phone digits from a WhatsApp JID (e.g. '919812345678@c.us' → '919812345678')."""
    return jid_to_phone(jid)


def _resolve_conversation(profile, jid: str, phone: str, display_name: str):
    """
    Get-or-create a Conversation keyed by whatsapp_jid.
    Updates phone and display_name if they changed.
    Returns None when neither jid nor phone is available.
    """
    from apps.sessions.models import Conversation

    jid = normalize_jid(jid, phone=phone)
    phone = (phone or "").strip()

    if not jid:
        return None

    # Derive phone from JID when not provided separately
    if not phone:
        phone = _jid_to_phone(jid)

    conversation, _ = Conversation.objects.get_or_create(
        profile=profile,
        whatsapp_jid=jid,
        defaults={"phone": phone, "display_name": display_name or ""},
    )

    # Keep phone and name fresh without an extra save if nothing changed
    updates = {}
    if phone and conversation.phone != phone:
        updates["phone"] = phone
    if display_name and conversation.display_name != display_name:
        updates["display_name"] = display_name
    if updates:
        for attr, val in updates.items():
            setattr(conversation, attr, val)
        conversation.save(update_fields=list(updates.keys()))

    return conversation


def _resolve_reply_phone(session: WhatsAppSession, sender_name: str,
                         sender_phone: str, jid: str = "") -> str:
    """
    Resolve the phone number to use for sending the reply.
    Priority: direct sender_phone → phone from JID → digits from name → Contact lookup.
    """
    if sender_phone:
        return sender_phone

    # Derive phone from JID — most reliable when extension extracted the JID
    phone_from_jid = _jid_to_phone(jid)
    if phone_from_jid:
        return phone_from_jid

    digits = re.sub(r"\D", "", sender_name)
    if len(digits) >= 7:
        return digits

    from apps.contacts.models import Contact

    owner = session.profile.owner
    contact = (
        Contact.objects.filter(owner=owner, name__iexact=sender_name).first()
        or Contact.objects.filter(owner=owner, name__icontains=sender_name).first()
    )
    return contact.phone_number if contact else ""


def _default_dispatch(profile_id: str, mode: str, target: str,
                      message: str, log_id: int, jid: str = "", conversation_id: int = None) -> None:
    """Push a JID-routed send task to the Chrome extension via Channels."""
    jid = normalize_jid(jid or target)
    if not validate_jid(jid):
        logger.warning(
            "[Routing] Rejecting send without valid JID - profile=%s target=%s log=%s",
            profile_id,
            target,
            log_id,
        )
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.warning("[Reply] Channel layer not configured - cannot push auto-reply")
        return

    task = {
        "type": "send_message_by_jid",
        "task_id": log_id,
        "message_id": log_id,
        "jid": jid,
        "message": message,
        "profile_id": profile_id,
        "conversation_id": conversation_id,
    }

    logger.info(
        "[Routing] Dispatching - profile=%s mode=jid target=%s phone=%s log=%s",
        profile_id,
        jid,
        jid_to_phone(jid) or "?",
        log_id,
    )

    async_to_sync(channel_layer.group_send)(
        f"profile_{profile_id}",
        {"type": "push_task", "task": task},
    )


# ?? Delayed reply dispatcher (runs in daemon thread) ??????????????????????????

def _fire_reply(
    profile_gologin_id: str,
    mode: str,
    reply_target: str,
    reply_body: str,
    sender_key: str,
    delay: int,
    incoming_id: int,
    jid: str = "",
    conversation_id: int = None,
    schedule_token: int = None,
) -> None:
    """
    Sleep for `delay` seconds, then create the MessageLog and push via channel layer.
    Uses channel layer so the correct consumer receives it regardless of which
    WebSocket connection is active at fire time.

    Routing is always via jid/phone — never active_chat DOM state.
    """
    time.sleep(delay)

    with _reply_lock:
        if schedule_token is not None and _reply_versions.get(sender_key) != schedule_token:
            _reply_pending.pop(sender_key, None)
            cache.delete(f"processing_lock:{profile_gologin_id}:{jid}")
            logger.info("[Reply] Delayed reply cancelled - newer message arrived sender_key=%s", sender_key)
            return
        _reply_pending.pop(sender_key, None)
    cache.delete(f"processing_lock:{profile_gologin_id}:{jid}")

    try:
        from apps.messaging.models import MessageLog
        from apps.profiles.models import GoLoginProfile

        profile_obj = GoLoginProfile.objects.get(gologin_profile_id=profile_gologin_id)
        log = MessageLog.objects.create(
            profile=profile_obj,
            phone_number=jid_to_phone(jid) or reply_target,
            whatsapp_jid=jid,
            message_body=reply_body,
            status=MessageLog.Status.PENDING,
        )
        logger.info("[Send] Opening target chat — jid=%s conversation=%s log=%s",
                    jid, conversation_id, log.id)
        from apps.realtime.consumers import push_task_to_profile

        push_task_to_profile(
            profile_gologin_id,
            jid_to_phone(jid) or reply_target,
            reply_body,
            log.id,
            jid=jid,
            conversation_id=conversation_id,
        )
        IncomingMessage.objects.filter(id=incoming_id).update(is_processed=True)
        logger.info("[Success] Message dispatched to jid=%s after %ds delay", jid or reply_target, delay)
    except Exception as exc:
        logger.warning("[Reply] Delayed dispatch failed — target=%s jid=%s error=%s",
                       reply_target, jid, exc)


# ── Public entry point ────────────────────────────────────────────────────────

def process_incoming_message(
    session: WhatsAppSession,
    sender_name: str,
    sender_phone: str = "",
    jid: str = "",
    preview: str = "",
    count: int = 1,
    dispatch=None,  # noqa: ARG001 — kept for API compatibility; channel layer always used
) -> str:
    """
    Record an incoming message and schedule a delayed auto-reply (REPLY_DELAY_MIN–MAX s).

    Routing is conversation-id / whatsapp_jid based — never active_chat state.

    Flow
    ────
    1. Ignore system / bad-name messages
    2. Dedup: skip if same preview already seen within DEDUP_WINDOW_MINUTES
    3. Per-sender guard: skip if a reply is already scheduled for this sender
    4. Resolve or create Conversation by whatsapp_jid
    5. Save IncomingMessage with jid + conversation FK
    6. Generate LLM reply now (so the LLM call is not inside the sleep thread)
    7. Resolve reply phone: sender_phone → jid digits → name lookup
    8. Schedule _fire_reply() in a daemon thread — sleeps 2-3 min, then dispatches

    Returns one of:
        'ignored' | 'duplicate' | 'pending' | 'no_reply' | 'no_target' | 'scheduled'
    """

    sender_name  = _clean_sender_name(sender_name)
    sender_phone = (sender_phone or "").strip()
    jid          = normalize_jid(jid, phone=sender_phone)
    preview      = (preview or "").strip()

    # If name couldn't be extracted but we have a phone, use phone as routing key
    if not sender_name and sender_phone:
        sender_name = sender_phone
    if not sender_phone and re.match(r"^\d{7,15}$", sender_name or ""):
        sender_phone = sender_name
    if not jid and sender_phone:
        jid = jid_from_phone(sender_phone)

    if _ignore_incoming(sender_name, preview):
        return "ignored"
    if not validate_jid(jid):
        logger.warning(
            "[Routing] Incoming rejected - missing valid jid profile=%s sender=%s phone=%s preview=%s",
            session.profile.gologin_profile_id,
            sender_name,
            sender_phone,
            preview[:60],
        )
        return "missing_jid"

    # ── Dedup: same preview from same sender within the window ───────────────
    cutoff = timezone.now() - timedelta(minutes=DEDUP_WINDOW_MINUTES)
    if IncomingMessage.objects.filter(
        session=session,
        sender_name=sender_name,
        message_preview=preview,
        received_at__gte=cutoff,
    ).exists():
        logger.debug("[Reply] Dedup skip — %s (same preview within %d min)", sender_name, DEDUP_WINDOW_MINUTES)
        return "duplicate"

    # ── Per-sender reply guard — keyed by jid when available, else name ───────
    sender_key = f"{session.profile.gologin_profile_id}:{jid}"
    processing_lock_key = f"processing_lock:{session.profile.gologin_profile_id}:{jid}"
    if not cache.add(processing_lock_key, "1", timeout=REPLY_DELAY_MAX + 300):
        with _reply_lock:
            if sender_key in _reply_pending:
                _reply_versions[sender_key] = _reply_versions.get(sender_key, 0) + 1
                _reply_pending.pop(sender_key, None)
                cache.delete(processing_lock_key)
                cache.add(processing_lock_key, "1", timeout=REPLY_DELAY_MAX + 300)
                logger.info(
                    "[Reply] Delayed reply cancelled - newer message arrived; scheduling replacement for %s",
                    jid or sender_name,
                )
            else:
                logger.info("[Reply] Redis processing lock active - key=%s", processing_lock_key)
                return "pending"

    with _reply_lock:
        if sender_key in _reply_pending:
            _reply_versions[sender_key] = _reply_versions.get(sender_key, 0) + 1
            logger.info("[Reply] New message arrived - cancelling older delayed reply for %s", jid or sender_name)
        else:
            _reply_versions[sender_key] = _reply_versions.get(sender_key, 0) + 1
        _reply_pending[sender_key] = True
        schedule_token = _reply_versions[sender_key]

    # ── Resolve stable Conversation ───────────────────────────────────────────
    conversation = _resolve_conversation(
        profile=session.profile,
        jid=jid,
        phone=sender_phone,
        display_name=sender_name,
    )
    conversation_id = conversation.id if conversation else None

    # ── Save incoming record ──────────────────────────────────────────────────
    incoming = IncomingMessage.objects.create(
        session=session,
        conversation=conversation,
        whatsapp_jid=jid,
        sender_name=sender_name,
        sender_phone=sender_phone,
        message_preview=preview,
        unread_count=max(int(count or 1), 1),
        received_at=timezone.now(),
    )
    logger.info("[Incoming] From %s — jid=%s conversation=%s preview=%s",
                sender_name, jid or "?", conversation_id, preview[:60])

    # Generate reply through the lead-qualification conversation manager.
    effective_preview = _effective_preview(sender_name, preview)
    reply = ""
    if conversation:
        try:
            from apps.autoreply.services.er import ConversationManager

            result = ConversationManager().handle_incoming(
                conversation=conversation,
                incoming_text=effective_preview or preview,
                sender_name=sender_name,
                external_message_id=f"incoming:{incoming.id}",
            )
            reply = result.get("reply", "") if result.get("status") == "reply" else ""
            if result.get("status") == "handoff":
                IncomingMessage.objects.filter(id=incoming.id).update(is_processed=True)
                with _reply_lock:
                    _reply_pending.pop(sender_key, None)
                cache.delete(processing_lock_key)
                return "handoff"
            if result.get("status") == "skipped":
                IncomingMessage.objects.filter(id=incoming.id).update(is_processed=True)
                with _reply_lock:
                    _reply_pending.pop(sender_key, None)
                cache.delete(processing_lock_key)
                return result.get("reason", "skipped")
        except Exception as exc:
            logger.warning("[Reply] Conversation manager failed for %s: %s", sender_name, exc)

    if not reply:
        from apps.autoreply.services.ai_generator import generate_safe_fallback_reply
        from apps.autoreply.services.language_detector import detect_language

        language = detect_language(effective_preview or preview, previous="english")
        reply = generate_safe_fallback_reply(effective_preview or preview, language=language)

    if not reply:
        with _reply_lock:
            _reply_pending.pop(sender_key, None)
        cache.delete(processing_lock_key)
        return "no_reply"

    # ── Resolve send target — jid/phone always preferred over name ────────────
    phone = jid_to_phone(jid)
    reply_target = jid
    if not validate_jid(jid):
        logger.info("[Reply] No valid jid for %s - skip", sender_name)
        with _reply_lock:
            _reply_pending.pop(sender_key, None)
        cache.delete(processing_lock_key)
        return "no_target"

    mode  = "jid"
    delay = random.randint(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
    profile_gologin_id = session.profile.gologin_profile_id

    logger.info("[Reply] Scheduling - profile=%s target=%s phone=%s conversation=%s mode=%s delay=%ds",
                profile_gologin_id, reply_target, phone or "?", conversation_id, mode, delay)

    threading.Thread(
        target=_fire_reply,
        args=(profile_gologin_id, mode, reply_target, reply, sender_key, delay,
              incoming.id, jid, conversation_id, schedule_token),
        daemon=True,
        name=f"reply-{sender_name[:12]}",
    ).start()

    return "scheduled"

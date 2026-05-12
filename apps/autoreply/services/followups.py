import logging
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from shared.utils.jid import normalize_jid

logger = logging.getLogger(__name__)

_FOLLOWUP_TZ = "Asia/Kolkata"
_FOLLOWUP_DAY_START = dt_time(8, 0)   # 8 AM
_FOLLOWUP_DAY_END   = dt_time(22, 0)  # 10 PM


def _is_business_hours() -> bool:
    """Return True if the current time is within 8 AM – 10 PM IST."""
    now = datetime.now(ZoneInfo(_FOLLOWUP_TZ)).time()
    return _FOLLOWUP_DAY_START <= now < _FOLLOWUP_DAY_END


def send_followup_messages():
    from apps.messaging.models import MessageLog
    from apps.realtime.consumers import push_task_to_profile
    from apps.sessions.models import IncomingMessage, WhatsAppSession
    from utils.ai_message import generate_followup

    if not _is_business_hours():
        logger.info("[Followup] Outside business hours — skipping run")
        return {"skipped": "outside_business_hours"}

    now = timezone.now()
    three_hours_ago = now - timedelta(hours=3)
    twenty_four_hours_ago = now - timedelta(hours=24)

    candidates = (
        MessageLog.objects
        .filter(
            campaign__isnull=True,
            status=MessageLog.Status.SENT,
            sent_at__lte=three_hours_ago,
            sent_at__gte=twenty_four_hours_ago,
        )
        .select_related("profile")
        .order_by("sent_at")
    )

    seen = set()
    for log in candidates:
        key = (log.profile_id, log.phone_number)
        if key in seen:
            continue
        seen.add(key)

        profile = log.profile
        if not profile or not profile.gologin_profile_id:
            continue

        if MessageLog.objects.filter(
            profile=profile,
            phone_number=log.phone_number,
            campaign__isnull=True,
            created_at__gt=log.created_at,
        ).exists():
            continue

        try:
            session = WhatsAppSession.objects.get(profile=profile)
        except WhatsAppSession.DoesNotExist:
            continue

        from django.db.models import Q
        if IncomingMessage.objects.filter(
            session=session,
            received_at__gt=log.sent_at,
        ).filter(
            Q(sender_phone=log.phone_number) | Q(sender_name=log.phone_number)
        ).exists():
            logger.info("[Followup] Skipped — contact replied after last message: %s", log.phone_number)
            continue

        # ── Build conversation context for a smarter follow-up ────────────────
        stage = ""
        last_question = ""
        last_user_message = ""
        try:
            from apps.sessions.models import Conversation
            from apps.autoreply.models import ConversationMessage, ConversationState

            conv = Conversation.objects.filter(
                profile=profile,
                whatsapp_jid=normalize_jid(log.whatsapp_jid, phone=log.phone_number),
            ).first()
            if conv:
                conv_state = ConversationState.objects.filter(conversation=conv).first()
                if conv_state:
                    stage = conv_state.stage or ""
                    last_question = conv_state.last_question_key or ""

                last_msg = (
                    ConversationMessage.objects
                    .filter(conversation=conv, sender="user")
                    .order_by("-created_at")
                    .first()
                )
                if last_msg:
                    last_user_message = (last_msg.text or "")[:120]
        except Exception as exc:
            logger.warning("[Followup] Context fetch failed for %s: %s", log.phone_number, exc)

        business_context = profile.business_context or ""
        follow_up_text = generate_followup(
            sender_name="",
            business_context=business_context,
            stage=stage,
            last_question=last_question,
            last_user_message=last_user_message,
        )

        follow_log = MessageLog.objects.create(
            profile=profile,
            phone_number=log.phone_number,
            whatsapp_jid=normalize_jid(log.whatsapp_jid, phone=log.phone_number),
            message_body=follow_up_text,
            status=MessageLog.Status.PENDING,
        )
        push_task_to_profile(
            profile.gologin_profile_id,
            log.phone_number,
            follow_up_text,
            follow_log.id,
            jid=follow_log.whatsapp_jid,
        )
        logger.info(
            "[Followup] Queued for %s — stage=%s last_question=%s",
            log.phone_number,
            stage or "unknown",
            last_question or "none",
        )

    return {"processed": len(seen)}

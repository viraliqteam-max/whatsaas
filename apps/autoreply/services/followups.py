import logging
from datetime import timedelta

from django.utils import timezone
from shared.utils.jid import normalize_jid

logger = logging.getLogger(__name__)


def send_followup_messages():
    from apps.messaging.models import MessageLog
    from apps.realtime.consumers import push_task_to_profile
    from apps.sessions.models import IncomingMessage, WhatsAppSession
    from utils.ai_message import generate_followup

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
            logger.info("Follow-up skipped - %s replied after auto-reply", log.phone_number)
            continue

        business_context = profile.business_context or ""
        follow_up_text = generate_followup("", business_context)

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
            "Follow-up queued for %s (original auto-reply sent at %s)",
            log.phone_number,
            log.sent_at,
        )

    return {"processed": len(seen)}

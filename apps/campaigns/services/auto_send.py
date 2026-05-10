import logging

from shared.utils.jid import normalize_jid

logger = logging.getLogger(__name__)


def auto_send_for_new_contact(contact) -> None:
    from apps.messaging.models import Campaign, MessageLog
    from utils.ai_message import generate_personalized_message

    campaigns = Campaign.objects.filter(
        owner=contact.owner,
        auto_send=True,
        status__in=[Campaign.Status.RUNNING, Campaign.Status.DRAFT],
    ).select_related("profile", "template")

    for campaign in campaigns:
        profile = campaign.profile
        if not profile or not profile.gologin_profile_id:
            logger.warning("Campaign %s has no valid profile - skipping auto-send", campaign.id)
            continue

        template_body = campaign.template.body if campaign.template else campaign.custom_message
        message_body = generate_personalized_message(
            template_body=template_body,
            contact_name=contact.name,
            contact_phone=contact.phone_number,
        )

        log = MessageLog.objects.create(
            campaign=campaign,
            profile=profile,
            contact=contact,
            phone_number=contact.phone_number,
            whatsapp_jid=normalize_jid(getattr(contact, "whatsapp_jid", ""), phone=contact.phone_number),
            message_body=message_body,
            status=MessageLog.Status.PENDING,
        )

        dispatch_auto_send(profile.gologin_profile_id, contact.phone_number, message_body, log.id, jid=log.whatsapp_jid)
        logger.info(
            "Auto-send queued: contact=%s campaign=%s log=%d",
            contact.phone_number,
            campaign.name,
            log.id,
        )


def dispatch_auto_send(profile_id: str, phone: str, message: str, log_id: int, jid: str = "") -> None:
    """Push a JID-routed send task to the extension."""
    try:
        from apps.realtime.consumers import push_task_to_profile

        push_task_to_profile(profile_id, phone, message, log_id, jid=jid)
        return
    except Exception as exc:
        logger.error("WebSocket push failed for auto-send log=%s profile=%s: %s", log_id, profile_id, exc)
        raise

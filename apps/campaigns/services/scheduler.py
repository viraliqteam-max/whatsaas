import logging

from django.utils import timezone
from shared.utils.jid import normalize_jid

logger = logging.getLogger(__name__)


def dispatch_due_campaigns(dry_run: bool = False):
    from apps.messaging.models import Campaign, MessageLog
    from apps.realtime.consumers import push_task_to_profile
    from utils.ai_message import generate_personalized_message

    now = timezone.now()
    due = Campaign.objects.filter(
        status__in=[Campaign.Status.DRAFT, Campaign.Status.SCHEDULED],
        scheduled_at__lte=now,
    ).select_related("profile", "template")

    results = []
    for campaign in due:
        profile = campaign.profile
        if not profile or not profile.gologin_profile_id:
            results.append({"campaign": campaign, "status": "skipped", "reason": "no_profile"})
            continue

        contacts = list(campaign.get_all_contacts().filter(is_active=True))
        if not contacts:
            results.append({"campaign": campaign, "status": "skipped", "reason": "no_contacts"})
            continue

        if dry_run:
            results.append(
                {
                    "campaign": campaign,
                    "status": "dry_run",
                    "contacts": len(contacts),
                    "profile_id": profile.gologin_profile_id,
                }
            )
            continue

        campaign.status = Campaign.Status.RUNNING
        campaign.started_at = now
        campaign.save(update_fields=["status", "started_at"])

        dispatched = 0
        for contact in contacts:
            template_body = campaign.template.body if campaign.template else campaign.custom_message
            message_body = generate_personalized_message(
                template_body=template_body or "",
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
            try:
                push_task_to_profile(
                    profile.gologin_profile_id,
                    contact.phone_number,
                    message_body,
                    log.id,
                    jid=log.whatsapp_jid,
                )
                dispatched += 1
            except Exception as exc:
                log.status = MessageLog.Status.FAILED
                log.error_message = str(exc)
                log.save(update_fields=["status", "error_message"])
                logger.warning("Push failed for %s: %s", contact.phone_number, exc)

        results.append(
            {
                "campaign": campaign,
                "status": "dispatched",
                "dispatched": dispatched,
                "contacts": len(contacts),
            }
        )

    return results

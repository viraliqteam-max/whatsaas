import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Contact

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Contact)
def auto_send_on_new_contact(sender, instance: Contact, created: bool, **kwargs):
    if not created:
        return

    try:
        from apps.campaigns.services.auto_send import auto_send_for_new_contact

        auto_send_for_new_contact(instance)
    except Exception as exc:
        logger.warning("Auto-send signal failed for contact=%s: %s", instance.id, exc)

from celery import shared_task
from django.utils import timezone
import logging

from apps.campaigns.services.execution import execute_campaign, send_single_message
from shared.constants.queues import RETRY_QUEUE

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="messaging.run_campaign", max_retries=3)
def run_campaign_task(self, campaign_id: int):
    result = execute_campaign(campaign_id, celery_task_id=self.request.id)
    if result.get("deferred"):
        raise self.retry(countdown=30, queue=RETRY_QUEUE)
    return result


@shared_task(bind=True, name="messaging.send_single_message", max_retries=3)
def send_single_message_task(
    self,
    profile_gologin_id: str,
    phone_number: str,
    message: str,
    log_id: int = None,
):
    result = send_single_message(profile_gologin_id, phone_number, message, log_id=log_id)
    if result.get("deferred"):
        raise self.retry(countdown=30, queue=RETRY_QUEUE)
    return result


@shared_task(bind=True, name="messaging.check_message_ack_timeout", max_retries=0)
def check_message_ack_timeout_task(self, log_id: int):
    from apps.messaging.models import MessageLog
    from apps.realtime.consumers import push_task_to_profile
    from shared.utils.jid import normalize_jid, validate_jid

    try:
        log = MessageLog.objects.select_related("profile").get(id=log_id)
    except MessageLog.DoesNotExist:
        return {"status": "not_found", "log_id": log_id}

    if log.status == MessageLog.Status.SENT:
        return {"status": "already_sent", "log_id": log_id}

    if log.status not in {
        MessageLog.Status.DISPATCHED,
        MessageLog.Status.EXTENSION_RECEIVED,
        MessageLog.Status.OPENING_CHAT,
        MessageLog.Status.SENDING,
        MessageLog.Status.ACK_RECEIVED,
        MessageLog.Status.RETRYING,
    }:
        return {"status": "ignored", "log_id": log_id, "message_status": log.status}

    jid = normalize_jid(log.whatsapp_jid, phone=log.phone_number)
    profile_id = getattr(log.profile, "gologin_profile_id", "") if log.profile_id else ""
    if not profile_id or not validate_jid(jid):
        log.status = MessageLog.Status.FAILED
        log.error_message = "ACK timeout: missing profile or valid JID"
        log.save(update_fields=["status", "error_message"])
        return {"status": "failed", "reason": log.error_message, "log_id": log_id}

    log.ack_timeout_attempts += 1
    if log.ack_timeout_attempts > 2:
        log.status = MessageLog.Status.FAILED
        log.error_message = "ACK missing after retry attempts"
        log.save(update_fields=["status", "error_message", "ack_timeout_attempts"])
        return {"status": "failed", "reason": log.error_message, "log_id": log_id}

    log.status = MessageLog.Status.RETRYING
    log.error_message = f"ACK timeout at {timezone.now().isoformat()} - retry {log.ack_timeout_attempts}/2"
    log.save(update_fields=["status", "error_message", "ack_timeout_attempts"])

    logger.warning(
        "[ACK] timeout retry - "
        f"profile={profile_id} jid={jid} message_id={log.id} "
        f"attempt={log.ack_timeout_attempts}/2"
    )
    push_task_to_profile(profile_id, log.phone_number, log.message_body, log.id, jid=jid)
    return {"status": "retrying", "log_id": log_id, "attempt": log.ack_timeout_attempts}

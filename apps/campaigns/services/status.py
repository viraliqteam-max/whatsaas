import logging

from django.utils import timezone
from shared.utils.jid import normalize_jid, validate_jid

logger = logging.getLogger(__name__)

UNRESOLVED_STATUSES = (
    "pending",
    "scheduled",
    "dispatched",
    "extension_received",
    "opening_chat",
    "sending",
    "ack_received",
    "retrying",
)

TERMINAL_STATUSES = (
    "sent",
    "failed",
    "skipped",
)


def _complete_campaign_if_ready(log) -> str:
    from apps.messaging.models import Campaign, MessageLog

    campaign = log.campaign
    if campaign and campaign.status == Campaign.Status.RUNNING:
        pending = campaign.message_logs.filter(status__in=UNRESOLVED_STATUSES).count()
        if pending == 0:
            campaign.status = Campaign.Status.COMPLETED
            campaign.completed_at = timezone.now()
            campaign.save(update_fields=["status", "completed_at"])
            logger.info("Campaign %s auto-completed", campaign.id)
            return "campaign_completed"
    return "updated"


def sync_campaign_runtime_status(campaign) -> bool:
    """
    Repair campaign runtime state after ACK/status updates.

    Returns True when the campaign row was changed.
    """
    from apps.messaging.models import Campaign, MessageLog

    if campaign.status != Campaign.Status.RUNNING:
        return False

    logs = campaign.message_logs.all()
    total = logs.count()
    if total == 0:
        logger.info("Campaign runtime status unchanged - campaign=%s reason=no_logs", campaign.id)
        return False

    unresolved = logs.filter(status__in=UNRESOLVED_STATUSES).count()
    if unresolved > 0:
        logger.info(
            "Campaign runtime status unchanged - campaign=%s total=%s unresolved=%s",
            campaign.id,
            total,
            unresolved,
        )
        return False

    campaign.status = Campaign.Status.COMPLETED
    campaign.completed_at = timezone.now()
    campaign.save(update_fields=["status", "completed_at"])
    logger.info("Campaign runtime status repaired - campaign=%s total=%s", campaign.id, total)
    return True


def mark_message_stage(log_id, stage: str, error: str = "") -> str:
    from apps.messaging.models import MessageLog

    allowed = {
        MessageLog.Status.DISPATCHED,
        MessageLog.Status.EXTENSION_RECEIVED,
        MessageLog.Status.OPENING_CHAT,
        MessageLog.Status.SENDING,
        MessageLog.Status.ACK_RECEIVED,
        MessageLog.Status.RETRYING,
        MessageLog.Status.FAILED,
    }
    if stage not in allowed:
        logger.warning("Message stage ignored - log=%s invalid_stage=%s", log_id, stage)
        return "invalid_stage"

    try:
        log = MessageLog.objects.get(id=int(log_id))
    except (MessageLog.DoesNotExist, TypeError, ValueError):
        logger.warning("MessageLog %s not found for stage %s", log_id, stage)
        return "not_found"

    if log.status == MessageLog.Status.SENT:
        logger.info("Message stage ignored after sent - log=%s stage=%s", log_id, stage)
        return "already_sent"

    log.status = stage
    if error:
        log.error_message = error
    log.save(update_fields=["status", "error_message"])
    logger.info(
        "Message stage updated - log=%s status=%s profile=%s jid=%s error=%s",
        log.id,
        stage,
        getattr(log.profile, "gologin_profile_id", "") if log.profile_id else "",
        log.whatsapp_jid,
        error or "",
    )
    return "updated"


def handle_message_ack(data: dict) -> str:
    from apps.messaging.models import MessageLog

    log_id = data.get("message_id") or data.get("task_id")
    status = (data.get("status") or "").strip().lower()
    jid = normalize_jid(data.get("jid", ""))
    error = data.get("error") or data.get("reason") or ""

    try:
        log = MessageLog.objects.select_related("campaign", "profile").get(id=int(log_id))
    except (MessageLog.DoesNotExist, TypeError, ValueError):
        logger.warning("ACK ignored - MessageLog %s not found payload=%s", log_id, data)
        return "not_found"

    if jid and validate_jid(jid) and normalize_jid(log.whatsapp_jid, phone=log.phone_number) != jid:
        logger.warning(
            "ACK jid mismatch - log=%s expected=%s received=%s profile=%s",
            log.id,
            log.whatsapp_jid,
            jid,
            data.get("profile_id", ""),
        )
        return "jid_mismatch"

    if log.status == MessageLog.Status.SENT:
        logger.info("Duplicate ACK ignored - log=%s jid=%s", log.id, log.whatsapp_jid)
        return "duplicate"

    if status == "sent":
        log.status = MessageLog.Status.ACK_RECEIVED
        log.error_message = ""
        log.save(update_fields=["status", "error_message"])
        logger.info(
            "[ACK] received - log=%s profile=%s jid=%s conversation=%s",
            log.id,
            data.get("profile_id", ""),
            log.whatsapp_jid,
            data.get("conversation_id", ""),
        )
        log.status = MessageLog.Status.SENT
        log.sent_at = timezone.now()
        log.save(update_fields=["status", "sent_at", "error_message"])
        logger.info("[DB] update result - log=%s status=sent sent_at=%s", log.id, log.sent_at)
        return _complete_campaign_if_ready(log)

    failure_statuses = {
        "open_chat_failed",
        "send_failed",
        "invalid_jid",
        "extension_timeout",
        "websocket_disconnected",
        "dom_not_loaded",
        "failed",
    }
    if status in failure_statuses:
        log.status = MessageLog.Status.FAILED
        log.error_message = error or status
        log.save(update_fields=["status", "error_message"])
        logger.warning("[Failure] ACK saved - log=%s jid=%s status=%s error=%s", log.id, log.whatsapp_jid, status, log.error_message)
        return _complete_campaign_if_ready(log)

    logger.warning("ACK ignored - log=%s unknown status=%s payload=%s", log.id, status, data)
    return "unknown_status"


def handle_message_result(log_id, success: bool, error: str = "") -> str:
    """
    Persist an extension send result and complete a campaign when all messages
    have resolved.
    """
    from apps.messaging.models import Campaign, MessageLog

    try:
        log = MessageLog.objects.select_related("campaign").get(id=int(log_id))
    except (MessageLog.DoesNotExist, TypeError, ValueError):
        logger.warning("MessageLog %s not found", log_id)
        return "not_found"

    payload = {
        "message_id": log_id,
        "jid": log.whatsapp_jid,
        "status": "sent" if success else "send_failed",
        "error": error or "",
    }
    return handle_message_ack(payload)

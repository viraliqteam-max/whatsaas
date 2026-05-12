from celery import shared_task
from django.core.cache import cache
from django.utils import timezone
import logging

from apps.campaigns.services.execution import execute_campaign, send_single_message
from shared.constants.queues import RETRY_QUEUE

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="messaging.run_campaign", max_retries=30)
def run_campaign_task(self, campaign_id: int):
    result = execute_campaign(campaign_id, celery_task_id=self.request.id)
    if result.get("deferred"):
        # 120s between retries (×30 = 60 min window); execute_campaign already
        # waited 5 min internally so this outer retry handles the rare very-slow-start case.
        raise self.retry(countdown=120, queue=RETRY_QUEUE)
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
    from datetime import timedelta
    from apps.messaging.models import MessageLog
    from apps.realtime.consumers import push_task_to_profile
    from shared.utils.jid import normalize_jid, validate_jid

    try:
        log = MessageLog.objects.select_related("profile").get(id=log_id)
    except MessageLog.DoesNotExist:
        return {"status": "not_found", "log_id": log_id}

    if log.status == MessageLog.Status.SENT:
        logger.info("[RetrySkipped] reason=already_sent log=%s jid=%s", log_id, log.whatsapp_jid)
        return {"status": "already_sent", "log_id": log_id}

    # Prevent concurrent workers from replaying the same task simultaneously
    message_lock_key = f"message_lock:{log_id}"
    if not cache.add(message_lock_key, "1", timeout=180):
        logger.info("[RetrySkipped] reason=message_lock log=%s jid=%s", log_id, log.whatsapp_jid)
        return {"status": "retry_skipped", "reason": "message_lock", "log_id": log_id}

    if log.status not in {
        MessageLog.Status.DISPATCHED,
        MessageLog.Status.EXTENSION_RECEIVED,
        MessageLog.Status.OPENING_CHAT,
        MessageLog.Status.SENDING,
        MessageLog.Status.ACK_RECEIVED,
        MessageLog.Status.RETRYING,
    }:
        cache.delete(message_lock_key)
        logger.info("[RetrySkipped] reason=invalid_status log=%s status=%s", log_id, log.status)
        return {"status": "ignored", "log_id": log_id, "message_status": log.status}

    jid = normalize_jid(log.whatsapp_jid, phone=log.phone_number)
    profile_id = getattr(log.profile, "gologin_profile_id", "") if log.profile_id else ""
    if not profile_id or not validate_jid(jid):
        log.status = MessageLog.Status.FAILED
        log.error_message = "ACK timeout: missing profile or valid JID"
        log.save(update_fields=["status", "error_message"])
        cache.delete(message_lock_key)
        return {"status": "failed", "reason": log.error_message, "log_id": log_id}

    # If the last failure was profile_not_ready (WhatsApp not connected), do NOT
    # count it against ack_timeout_attempts — the message was never actually sent.
    # First check if the profile has recovered since the error was recorded.
    # If still not ready: reschedule and wait. If recovered: clear the stale error
    # and fall through to normal dispatch so the message goes out immediately.
    if (log.error_message or "").startswith("profile_not_ready:"):
        if log.created_at < timezone.now() - timedelta(hours=2):
            log.status = MessageLog.Status.FAILED
            log.error_message = "Profile never came online — gave up after 2 hours"
            log.save(update_fields=["status", "error_message"])
            cache.delete(message_lock_key)
            logger.warning("[RetryFailed] reason=profile_never_ready log=%s jid=%s", log_id, jid)
            return {"status": "failed", "reason": "profile_never_ready", "log_id": log_id}

        from apps.profiles.services import profile_ready_for_dispatch
        try:
            now_ready, _ = profile_ready_for_dispatch(profile_id)
        except Exception:
            now_ready = False

        if not now_ready:
            # Re-trigger browser launch every 3 minutes so a crashed or never-started
            # browser gets relaunched without waiting for a new campaign preflight.
            try:
                from apps.profiles.tasks import ensure_profile_runtime_task as _ert
                _ensure_key = f"ensure_runtime_scheduled:{profile_id}"
                if not cache.get(_ensure_key):
                    cache.set(_ensure_key, "1", timeout=180)
                    _ert.apply_async(args=[profile_id, "ack_timeout_recovery"], countdown=5)
            except Exception as _exc:
                logger.warning("[RetryWait] ensure_runtime_skipped log=%s error=%s", log_id, _exc)
            cache.delete(message_lock_key)
            logger.info("[RetryWait] profile_not_ready log=%s jid=%s — rescheduling in 120s", log_id, jid)
            check_message_ack_timeout_task.apply_async(args=[log_id], countdown=120)
            return {"status": "waiting_for_profile", "log_id": log_id}

        # Profile has recovered — clear stale error and fall through to dispatch
        logger.info("[RetryRecover] profile_recovered log=%s jid=%s — dispatching now", log_id, jid)
        log.error_message = ""
        log.save(update_fields=["error_message"])

    log.ack_timeout_attempts += 1
    max_attempts = 5
    if log.ack_timeout_attempts > max_attempts:
        log.status = MessageLog.Status.FAILED
        log.error_message = "ACK missing after retry attempts"
        log.save(update_fields=["status", "error_message", "ack_timeout_attempts"])
        cache.delete(message_lock_key)
        logger.warning("[RetrySkipped] reason=max_attempts log=%s jid=%s attempts=%d",
                       log_id, log.whatsapp_jid, log.ack_timeout_attempts)
        return {"status": "failed", "reason": log.error_message, "log_id": log_id}

    log.status = MessageLog.Status.RETRYING
    log.error_message = f"ACK timeout at {timezone.now().isoformat()} - retry {log.ack_timeout_attempts}/{max_attempts}"
    log.save(update_fields=["status", "error_message", "ack_timeout_attempts"])

    logger.warning(
        "[Retry] ACK timeout retry - "
        f"profile={profile_id} jid={jid} message_id={log.id} "
        f"attempt={log.ack_timeout_attempts}/{max_attempts}"
    )
    push_task_to_profile(profile_id, log.phone_number, log.message_body, log.id, jid=jid)
    # Release lock after dispatch so the next ACK-timeout can re-enter if needed
    cache.delete(message_lock_key)
    return {"status": "retrying", "log_id": log_id, "attempt": log.ack_timeout_attempts}


@shared_task(name="messaging.reconcile_stale_messages", max_retries=0)
def reconcile_stale_messages_task():
    """
    Scan MessageLogs stuck in unresolved statuses for >10 min and heal them.
    Runs every 60 s via Celery Beat.

    - RETRYING with attempts < max  → re-dispatch
    - RETRYING with attempts >= max → mark FAILED
    - DISPATCHED / SENDING stuck    → schedule ACK-timeout retry
    - Any stale with no valid JID   → mark FAILED immediately

    Uses created_at for stale detection because MessageLog has no updated_at field.
    created_at is set at dispatch time, so any log still in-flight after STALE_AFTER_MINUTES
    from creation is genuinely stuck.
    """
    from datetime import timedelta
    from apps.messaging.models import MessageLog
    from shared.utils.jid import normalize_jid, validate_jid

    STALE_AFTER_MINUTES = 10
    MAX_ATTEMPTS = 5
    cutoff = timezone.now() - timedelta(minutes=STALE_AFTER_MINUTES)

    try:
        stale = MessageLog.objects.filter(
            status__in=[
                MessageLog.Status.DISPATCHED,
                MessageLog.Status.EXTENSION_RECEIVED,
                MessageLog.Status.OPENING_CHAT,
                MessageLog.Status.SENDING,
                MessageLog.Status.ACK_RECEIVED,
                MessageLog.Status.RETRYING,
            ],
            created_at__lt=cutoff,
        ).select_related("profile").order_by("id")[:100]
        # Evaluate the queryset now so any ORM error surfaces here, not mid-loop
        stale = list(stale)
    except Exception as exc:
        logger.error("[Reconcile] Query failed — aborting: %s", exc)
        return {"error": str(exc)}

    fixed = skipped = 0
    for log in stale:
        profile_id = getattr(log.profile, "gologin_profile_id", "") if log.profile_id else ""
        jid = normalize_jid(log.whatsapp_jid, phone=log.phone_number)

        logger.info("[Reconcile] log=%s profile=%s jid=%s status=%s attempts=%d",
                    log.id, profile_id, jid, log.status, log.ack_timeout_attempts)

        if not profile_id or not validate_jid(jid):
            log.status = MessageLog.Status.FAILED
            log.error_message = "Reconcile: missing profile or invalid JID"
            log.save(update_fields=["status", "error_message"])
            logger.warning("[ReconcileFixed] log=%s status=failed reason=missing_jid_or_profile", log.id)
            fixed += 1
            continue

        if log.ack_timeout_attempts >= MAX_ATTEMPTS:
            log.status = MessageLog.Status.FAILED
            log.error_message = f"Reconcile: stale after {MAX_ATTEMPTS} retry attempts"
            log.save(update_fields=["status", "error_message"])
            logger.warning("[ReconcileFixed] log=%s status=failed reason=max_retries jid=%s", log.id, jid)
            fixed += 1
            continue

        # Schedule an ACK-timeout retry — re-uses existing retry logic + dedup locks
        try:
            check_message_ack_timeout_task.apply_async(args=[log.id], countdown=5)
            logger.info("[ReconcileFixed] log=%s jid=%s queued=ack_timeout_retry", log.id, jid)
            fixed += 1
        except Exception as exc:
            logger.warning("[ReconcileSkipped] log=%s jid=%s error=%s", log.id, jid, exc)
            skipped += 1

    logger.info("[Reconcile] complete fixed=%d skipped=%d", fixed, skipped)
    return {"fixed": fixed, "skipped": skipped}

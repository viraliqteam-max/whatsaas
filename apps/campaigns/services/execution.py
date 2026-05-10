import logging
import random
import time
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone
from shared.utils.jid import normalize_jid

logger = logging.getLogger(__name__)

DEFAULT_WINDOWS = [
    {"start": "08:00", "end": "11:00"},
    {"start": "13:00", "end": "16:00"},
    {"start": "19:00", "end": "22:00"},
]


def parse_hhmm(t_str: str) -> dt_time:
    h, m = t_str.split(":")
    return dt_time(int(h), int(m))


def is_in_window(now_time: dt_time, windows: list) -> bool:
    for window in windows:
        start = parse_hhmm(window["start"])
        end = parse_hhmm(window["end"])
        if start <= now_time < end:
            return True
    return False


def wait_for_window(windows: list, tz_name: str) -> None:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone '%s', falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")

    while True:
        now = datetime.now(tz)
        if is_in_window(now.time(), windows):
            return

        sorted_windows = sorted(windows, key=lambda w: w["start"])
        now_hhmm = now.strftime("%H:%M")

        next_start_str = None
        for window in sorted_windows:
            if window["start"] > now_hhmm:
                next_start_str = window["start"]
                break

        if next_start_str:
            h, m = next_start_str.split(":")
            next_dt = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        else:
            h, m = sorted_windows[0]["start"].split(":")
            tomorrow = now.date() + timedelta(days=1)
            next_dt = datetime(
                tomorrow.year,
                tomorrow.month,
                tomorrow.day,
                int(h),
                int(m),
                tzinfo=tz,
            )

        wait_secs = (next_dt - now).total_seconds()
        logger.info(
            "Outside sending window. Next window starts at %s (%s). "
            "Waiting %.0f seconds (checking every 5 min).",
            next_dt.strftime("%H:%M"),
            tz_name,
            wait_secs,
        )
        time.sleep(min(wait_secs, 300))


def random_delay(min_sec: int, max_sec: int) -> None:
    delay = random.randint(min_sec, max_sec)
    logger.info("Waiting %d seconds before next message.", delay)
    time.sleep(delay)


def execute_campaign(campaign_id: int, celery_task_id: str = ""):
    """
    Execute a campaign through Selenium.

    This is intentionally still synchronous inside the worker process. The
    important boundary is that campaign orchestration now belongs to the
    campaigns module, so future queue separation can happen without touching
    messaging models or API routes.
    """
    from apps.messaging.models import Campaign, MessageLog
    from apps.realtime.consumers import push_task_to_profile

    try:
        campaign = Campaign.objects.select_related("profile", "template").get(id=campaign_id)
    except Campaign.DoesNotExist:
        logger.error("Campaign %s not found", campaign_id)
        return {"error": "Campaign not found"}

    active_logs = campaign.message_logs.filter(
        status__in=[
            MessageLog.Status.PENDING,
            MessageLog.Status.SCHEDULED,
            MessageLog.Status.DISPATCHED,
            MessageLog.Status.EXTENSION_RECEIVED,
            MessageLog.Status.OPENING_CHAT,
            MessageLog.Status.SENDING,
            MessageLog.Status.ACK_RECEIVED,
            MessageLog.Status.RETRYING,
        ]
    ).exists()
    if campaign.status == Campaign.Status.RUNNING and not active_logs:
        logger.warning("Campaign %s was running with no active logs - allowing restart", campaign_id)
    elif campaign.status not in (Campaign.Status.DRAFT, Campaign.Status.SCHEDULED):
        logger.warning("Campaign %s is already %s - skipping", campaign_id, campaign.status)
        return {"skipped": True}

    campaign.status = Campaign.Status.RUNNING
    campaign.started_at = timezone.now()
    if celery_task_id:
        campaign.celery_task_id = celery_task_id
        campaign.save(update_fields=["status", "started_at", "celery_task_id"])
    else:
        campaign.save(update_fields=["status", "started_at"])

    profile = campaign.profile
    if not profile or not profile.gologin_profile_id:
        campaign.status = Campaign.Status.FAILED
        campaign.save(update_fields=["status"])
        return {"error": "No valid GoLogin profile attached to this campaign"}

    windows = campaign.allowed_time_windows or DEFAULT_WINDOWS
    tz_name = campaign.campaign_timezone or "Asia/Kolkata"
    min_wait = max(campaign.min_delay_seconds, 1)
    max_wait = max(campaign.max_delay_seconds, min_wait)

    contacts = list(campaign.get_all_contacts().filter(is_active=True))
    dispatched, failed = 0, 0
    total = len(contacts)

    for index, contact in enumerate(contacts):
        campaign.refresh_from_db(fields=["status"])
        if campaign.status == Campaign.Status.PAUSED:
            logger.info("Campaign %s paused at contact %d/%d", campaign_id, index + 1, total)
            return {"paused": True, "dispatched": dispatched, "failed": failed}
        if campaign.status == Campaign.Status.FAILED:
            return {"aborted": True, "dispatched": dispatched, "failed": failed}

        if campaign.respect_time_windows:
            wait_for_window(windows, tz_name)

        message_body = campaign.get_message_for(contact)
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
            jid = normalize_jid(getattr(contact, "whatsapp_jid", ""), phone=contact.phone_number)
            push_task_to_profile(profile.gologin_profile_id, contact.phone_number, message_body, log.id, jid=jid)
            dispatched += 1
            logger.info("Campaign task emitted to %s jid=%s (%d/%d)", contact.phone_number, jid, index + 1, total)
        except Exception as exc:
            log.status = MessageLog.Status.FAILED
            log.error_message = str(exc)
            log.save(update_fields=["status", "error_message"])
            failed += 1
            logger.warning("Failed to dispatch to %s: %s", contact.phone_number, log.error_message)

        if index < total - 1:
            random_delay(min_wait, max_wait)

    summary = {
        "campaign_id": campaign_id,
        "dispatched": dispatched,
        "failed": failed,
        "total": total,
    }
    logger.info("Campaign %s dispatch completed: %s", campaign_id, summary)
    return summary


def send_single_message(profile_gologin_id: str, phone_number: str, message: str, log_id: int = None):
    from apps.messaging.models import MessageLog
    from apps.realtime.consumers import push_task_to_profile
    from shared.utils.jid import normalize_jid

    try:
        jid = ""
        if log_id:
            log = MessageLog.objects.filter(id=log_id).first()
            jid = normalize_jid(getattr(log, "whatsapp_jid", ""), phone=phone_number)
        push_task_to_profile(profile_gologin_id, phone_number, message, log_id, jid=jid)
        return {"success": True, "dispatched": True}
    except Exception as exc:
        if log_id:
            MessageLog.objects.filter(id=log_id).update(status=MessageLog.Status.FAILED, error_message=str(exc))
        return {"success": False, "error": str(exc)}

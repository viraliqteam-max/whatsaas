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


def compute_smart_delay(
    send_index: int,
    total_contacts: int,
    profile_health: str = "healthy",
    min_override: int = 0,
    max_override: int = 0,
) -> int:
    """
    Compute a human-like delay between messages.

    Base range scales with send volume to avoid ban triggers:
    - First 5 sends: 90-150s (warm-up)
    - Sends 6-20: 120-200s (normal pace)
    - Sends 21+: 150-240s (safer pace)

    Adds 20% random jitter and reduces slightly during high-health runtimes.
    """
    if min_override and max_override:
        return random.randint(min_override, max_override)

    if send_index < 5:
        base_min, base_max = 90, 150
    elif send_index < 20:
        base_min, base_max = 120, 200
    else:
        base_min, base_max = 150, 240

    if profile_health == "healthy":
        base_min = int(base_min * 0.85)

    jitter = random.uniform(0.85, 1.20)
    delay = int(random.randint(base_min, base_max) * jitter)
    return max(delay, 60)


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

    # Hard guard: never re-execute a campaign that is already past the DRAFT/SCHEDULED state.
    # The previous "restart if RUNNING with no active_logs" path caused duplicate MessageLog
    # creation for all contacts when the campaign completed but status hadn't flipped to
    # COMPLETED yet (race between last ACK and run_campaign_task retry).
    if campaign.status not in (Campaign.Status.DRAFT, Campaign.Status.SCHEDULED):
        logger.warning(
            "Campaign %s is already %s — rejecting duplicate execution",
            campaign_id, campaign.status,
        )
        return {"skipped": True, "reason": campaign.status}

    profile = campaign.profile
    if not profile or not profile.gologin_profile_id:
        campaign.status = Campaign.Status.FAILED
        campaign.save(update_fields=["status"])
        return {"error": "No valid GoLogin profile attached to this campaign"}

    # Pre-flight: check profile readiness before setting RUNNING or creating any MessageLogs.
    # If not ready, return {"deferred": True} so run_campaign_task retries the whole campaign
    # in 30 s (campaign stays SCHEDULED). This prevents 100+ MessageLogs being created at once
    # when WhatsApp is offline, which caused an unbounded queue of per-message retry tasks and
    # the endless GoLogin browser-relaunch loop.
    from apps.profiles.services import profile_ready_for_dispatch
    from apps.profiles.tasks import ensure_profile_runtime_task
    from django.core.cache import cache as _exec_cache

    try:
        ready, preflight_reason = profile_ready_for_dispatch(profile.gologin_profile_id)
    except Exception as exc:
        logger.warning("[Campaign] preflight_check_error campaign=%s error=%s — proceeding", campaign_id, exc)
        ready = True  # don't block the campaign if the readiness check itself fails

    if not ready:
        _ensure_key = f"ensure_runtime_scheduled:{profile.gologin_profile_id}"
        if not _exec_cache.get(_ensure_key):
            _exec_cache.set(_ensure_key, "1", timeout=180)  # 3-min rate limit (was 10 min)
            ensure_profile_runtime_task.apply_async(
                args=[profile.gologin_profile_id, f"campaign_preflight:{preflight_reason}"],
                countdown=3,
            )
        logger.warning(
            "[Campaign] preflight_blocked campaign=%s profile=%s reason=%s — waiting up to 5 min",
            campaign_id, profile.gologin_profile_id, preflight_reason,
        )
        # Wait up to 5 minutes for the browser to launch and WhatsApp to connect.
        # Browser start + WA load typically takes 1-4 minutes; polling every 15s keeps
        # the worker alive without hammering the DB.
        _waited = 0
        while _waited < 300 and not ready:
            time.sleep(15)
            _waited += 15
            try:
                ready, preflight_reason = profile_ready_for_dispatch(profile.gologin_profile_id)
            except Exception:
                ready = False
            if ready:
                logger.info(
                    "[Campaign] profile came online after %ds wait campaign=%s",
                    _waited, campaign_id,
                )
                break

        if not ready:
            logger.warning(
                "[Campaign] preflight_blocked campaign=%s profile=%s reason=%s — deferring after 5min wait",
                campaign_id, profile.gologin_profile_id, preflight_reason,
            )
            return {"deferred": True, "reason": preflight_reason}
        # Profile is now ready — fall through to campaign execution

    campaign.status = Campaign.Status.RUNNING
    campaign.started_at = timezone.now()
    if celery_task_id:
        campaign.celery_task_id = celery_task_id
        campaign.save(update_fields=["status", "started_at", "celery_task_id"])
    else:
        campaign.save(update_fields=["status", "started_at"])

    windows = campaign.allowed_time_windows or DEFAULT_WINDOWS
    tz_name = campaign.campaign_timezone or "Asia/Kolkata"
    min_wait = max(campaign.min_delay_seconds, 1)
    max_wait = max(campaign.max_delay_seconds, min_wait)

    contacts = list(campaign.get_all_contacts().filter(is_active=True))
    dispatched, failed = 0, 0
    total = len(contacts)

    # Resolve profile health for smart delay calculation
    profile_health = getattr(profile, "health_status", "unknown")

    for index, contact in enumerate(contacts):
        campaign.refresh_from_db(fields=["status"])
        if campaign.status == Campaign.Status.PAUSED:
            logger.info("Campaign %s paused at contact %d/%d", campaign_id, index + 1, total)
            return {"paused": True, "dispatched": dispatched, "failed": failed}
        if campaign.status == Campaign.Status.FAILED:
            return {"aborted": True, "dispatched": dispatched, "failed": failed}

        if campaign.respect_time_windows:
            wait_for_window(windows, tz_name)

        # Idempotency guard: if this contact already has a non-failed MessageLog for this
        # campaign (from a concurrent/retried task run), skip to avoid duplicate sends.
        if MessageLog.objects.filter(
            campaign=campaign,
            profile=profile,
            phone_number=contact.phone_number,
        ).exclude(status=MessageLog.Status.FAILED).exists():
            logger.info(
                "[CampaignIdempotent] Skipping duplicate log — campaign=%s contact=%s",
                campaign_id, contact.phone_number,
            )
            dispatched += 1
            continue

        # AI mode: generate a unique contextual message per contact
        if getattr(campaign, "ai_mode", False):
            try:
                from utils.ai_message import generate_outreach_message
                business_context = getattr(profile, "business_context", "") or ""
                contact_tags = list(getattr(contact, "tags", None) or [])
                contact_notes = getattr(contact, "notes", "") or ""
                hint = campaign.custom_message or (campaign.template.body if campaign.template else "")
                message_body = generate_outreach_message(
                    contact_name=contact.name,
                    contact_phone=contact.phone_number,
                    intent=getattr(campaign, "campaign_intent", "outreach"),
                    business_context=business_context,
                    hint=hint,
                    tags=contact_tags,
                    notes=contact_notes,
                )
                logger.info(
                    "[CampaignAI] generated message campaign=%s contact=%s intent=%s",
                    campaign_id, contact.phone_number, campaign.campaign_intent,
                )
            except Exception as exc:
                logger.warning("[CampaignAI] generation failed campaign=%s contact=%s error=%s — using template", campaign_id, contact.phone_number, exc)
                message_body = campaign.get_message_for(contact)
        else:
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
            dispatch_result = push_task_to_profile(profile.gologin_profile_id, contact.phone_number, message_body, log.id, jid=jid)
            if dispatch_result is False:
                # Profile went offline mid-campaign — stop dispatching remaining contacts.
                # The already-RETRYING log will be auto-healed by check_message_ack_timeout_task.
                # Set campaign back to SCHEDULED so run_campaign_task retries remaining contacts.
                campaign.status = Campaign.Status.SCHEDULED
                campaign.save(update_fields=["status"])
                logger.warning(
                    "[CampaignDefer] profile_offline mid_campaign=%s contact=%d/%d — deferring remaining",
                    campaign_id, index + 1, total,
                )
                return {"deferred": True, "reason": "profile_not_ready_mid_dispatch", "dispatched": dispatched, "failed": failed}
            dispatched += 1
            logger.info("Campaign task emitted to %s jid=%s (%d/%d)", contact.phone_number, jid, index + 1, total)
        except Exception as exc:
            log.status = MessageLog.Status.FAILED
            log.error_message = str(exc)
            log.save(update_fields=["status", "error_message"])
            failed += 1
            logger.warning("Failed to dispatch to %s: %s", contact.phone_number, log.error_message)

        if index < total - 1:
            # AI campaigns use smart auto-delay; non-AI campaigns use manual settings
            if getattr(campaign, "ai_mode", False):
                smart = compute_smart_delay(
                    send_index=dispatched,
                    total_contacts=total,
                    profile_health=profile_health,
                )
                logger.info("Smart delay %ds before next send (ai_mode).", smart)
                time.sleep(smart)
            else:
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

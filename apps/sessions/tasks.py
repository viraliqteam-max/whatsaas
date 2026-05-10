"""
Celery tasks for WhatsApp session management.

poll_all_inboxes_task — runs every 30 seconds via Celery Beat.
  For every logged-in session it:
    1. Checks if the Chrome driver is alive.
    2. If Chrome crashed → relaunches the GoLogin profile automatically.
    3. After relaunch → checks if WhatsApp is still logged in or needs a QR scan.
    4. If logged in → scans the sidebar for unread messages and saves them.
"""
import logging
import time as time_mod
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="sessions.send_followup_messages")
def send_followup_messages_task():
    """
    Send a follow-up to contacts who received an auto-reply 3+ hours ago
    and still haven't replied back.  Should run every 30 minutes via Celery Beat.

    To activate:
      Django Admin → Periodic Tasks → Add
        Task: sessions.send_followup_messages
        Interval: every 30 minutes
    """
    from apps.autoreply.services.followups import send_followup_messages

    return send_followup_messages()
@shared_task(name="sessions.poll_all_inboxes")
def poll_all_inboxes_task():
    """
    Scan WhatsApp Web inbox for all sessions that are marked as logged_in.
    Called automatically by Celery Beat every 30 seconds.

    To activate:
      Go to Django Admin → Periodic Tasks → Add
        Task: sessions.poll_all_inboxes
        Interval: every 30 seconds
    """
    from apps.autoreply.queues.incoming_queue import enqueue_incoming_message
    from apps.sessions.models import WhatsAppSession, IncomingMessage
    from shared.services.session_locks import SessionLockUnavailable, session_lock
    from utils import gologin_manager as gl
    from utils import whatsapp_automation as wa
    from utils import wa_watcher
    from selenium.common.exceptions import WebDriverException

    # Only process sessions that were last known to be logged in
    sessions = WhatsAppSession.objects.filter(
        status=WhatsAppSession.Status.LOGGED_IN
    ).select_related("profile")

    logger.info("poll_all_inboxes: checking %d logged-in session(s)", sessions.count())

    for session in sessions:
        profile = session.profile

        if not profile.gologin_profile_id:
            logger.warning("Session %d has no GoLogin profile ID — skipping", session.id)
            continue

        driver = gl.get_active_driver(profile.gologin_profile_id)

        # ── Chrome is not running ──────────────────────────────────────────
        if driver is None:
            logger.info(
                "Chrome not running for profile '%s'. Attempting auto-relaunch...",
                profile.name
            )
            try:
                with session_lock(profile.gologin_profile_id, owner=f"poll-launch:{session.id}", timeout=60):
                    driver = gl.launch_profile(profile.gologin_profile_id)
                    time_mod.sleep(5)   # give Chrome time to fully start
            except SessionLockUnavailable as exc:
                logger.info("Skipping auto-relaunch for profile '%s': %s", profile.name, exc)
                continue
            except Exception as exc:
                # GoLogin API failed or Chrome could not start
                logger.error(
                    "Auto-relaunch failed for profile '%s': %s", profile.name, exc
                )
                session.status        = WhatsAppSession.Status.ERROR
                session.error_message = f"Auto-relaunch failed: {exc}"
                session.last_checked_at = timezone.now()
                session.save(update_fields=["status", "error_message", "last_checked_at"])
                continue

            # Chrome is back — check if WhatsApp session is still valid
            try:
                with session_lock(profile.gologin_profile_id, owner=f"poll-status:{session.id}", timeout=60):
                    result = wa.get_status(driver, navigate=True)
            except SessionLockUnavailable as exc:
                logger.info("Skipping status check for profile '%s': %s", profile.name, exc)
                continue
            session.status          = result["status"]
            session.qr_code_base64  = result.get("qr_code_base64") or ""
            session.last_checked_at = timezone.now()

            if result["status"] == WhatsAppSession.Status.LOGGED_IN:
                session.save(update_fields=["status", "qr_code_base64", "last_checked_at"])
                logger.info("Profile '%s' relaunched and still logged in.", profile.name)
                # Fall through to poll the inbox below
            else:
                # Session expired — user must re-scan QR code
                session.error_message = (
                    "WhatsApp session expired after Chrome restart. "
                    f"Re-scan QR at GET /api/sessions/{session.id}/check_status/"
                )
                session.save(update_fields=[
                    "status", "qr_code_base64", "last_checked_at", "error_message"
                ])
                logger.warning(
                    "Profile '%s' needs QR re-scan (status: %s). "
                    "Call /api/sessions/%d/check_status/ to get QR code.",
                    profile.name, result["status"], session.id
                )
                continue

        # ── Chrome is running — poll the inbox ────────────────────────────
        # If the Playwright WAWatcher is active for this profile it already
        # handles incoming messages in real time via a MutationObserver, so
        # there is no need for the Selenium poll here.  Fall back to the
        # Selenium path only when the watcher is not running (e.g. playwright
        # not installed, or the watcher thread crashed).
        if wa_watcher.is_watching(profile.gologin_profile_id):
            logger.debug(
                "poll_all_inboxes: WAWatcher active for '%s' — skipping Selenium poll",
                profile.name,
            )
            session.last_checked_at = timezone.now()
            session.save(update_fields=["last_checked_at"])
            continue

        try:
            try:
                with session_lock(profile.gologin_profile_id, owner=f"poll-inbox:{session.id}", timeout=60):
                    unread_chats = wa.poll_inbox(driver)
            except SessionLockUnavailable as exc:
                logger.info("Skipping inbox poll for profile '%s': %s", profile.name, exc)
                continue
        except WebDriverException as exc:
            # Driver threw an exception — Chrome crashed between the check and poll
            logger.error(
                "Chrome crashed during polling for profile '%s': %s", profile.name, exc
            )
            session.status        = WhatsAppSession.Status.ERROR
            session.error_message = f"Chrome crashed during polling: {exc}"
            session.last_checked_at = timezone.now()
            session.save(update_fields=["status", "error_message", "last_checked_at"])
            continue

        # Save each unread chat as an IncomingMessage
        now = timezone.now()
        new_count = 0
        for chat in unread_chats:
            try:
                result = enqueue_incoming_message(
                    session.id,
                    {
                        "sender_name": chat.get("sender_name", ""),
                        "sender_phone": chat.get("sender_phone", ""),
                        "jid": chat.get("jid", ""),
                        "preview": chat.get("message_preview", ""),
                        "count": chat.get("unread_count", 1),
                    },
                )
                if result == "queued":
                    new_count += 1
            except Exception as exc:
                logger.warning("Auto-reply error while polling '%s': %s", profile.name, exc)
            continue

            # Avoid flooding the DB: skip if we already have an unprocessed
            # message from this sender in the last 2 minutes
            already_exists = IncomingMessage.objects.filter(
                session=session,
                sender_name=chat["sender_name"],
                is_processed=False,
                received_at__gte=now - timezone.timedelta(minutes=2),
            ).exists()

            if not already_exists:
                IncomingMessage.objects.create(
                    session=session,
                    sender_name=chat["sender_name"],
                    message_preview=chat["message_preview"],
                    unread_count=chat["unread_count"],
                    received_at=now,
                )
                new_count += 1

        session.last_checked_at = now
        session.save(update_fields=["last_checked_at"])

        if new_count:
            logger.info(
                "Processed %d incoming auto-reply message(s) for profile '%s'.",
                new_count, profile.name
            )

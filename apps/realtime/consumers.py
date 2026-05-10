"""
WebSocket consumer — one connection per Chrome extension / AdsPower profile.

Flow:
  Extension connects  →  register message with profile_id
                      →  replay any PENDING MessageLogs (tasks missed while offline)
  Django pushes task  →  extension receives "send_message" command
  Extension replies   →  message_result / incoming_messages event

Routing is conversation_id / whatsapp_jid based — active_chat is never used.
"""
import json
import logging
import re

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer
from shared.utils.jid import jid_to_phone, normalize_jid, validate_jid

logger = logging.getLogger(__name__)


class AgentConsumer(WebsocketConsumer):

    def connect(self):
        self.profile_id = self.scope["url_route"]["kwargs"]["profile_id"]
        self.group_name = f"profile_{self.profile_id}"

        async_to_sync(self.channel_layer.group_add)(self.group_name, self.channel_name)
        self.accept()
        logger.info("Extension connected — profile %s", self.profile_id)

        # Auto-create GoLoginProfile + WhatsAppSession on first connect
        try:
            from django.contrib.auth.models import User
            from apps.profiles.models import GoLoginProfile
            from apps.sessions.models import WhatsAppSession

            owner = User.objects.filter(is_superuser=True).first()
            profile, p_created = GoLoginProfile.objects.get_or_create(
                gologin_profile_id=self.profile_id,
                defaults={"name": f"Profile {self.profile_id}", "owner": owner},
            )
            if p_created:
                logger.info("Auto-created GoLoginProfile for %s", self.profile_id)

            session, s_created = WhatsAppSession.objects.get_or_create(
                profile=profile,
                defaults={"status": WhatsAppSession.Status.LOGGED_IN},
            )
            if not s_created and session.status != WhatsAppSession.Status.LOGGED_IN:
                session.status = WhatsAppSession.Status.LOGGED_IN
                session.save(update_fields=["status"])
            if s_created:
                logger.info("Auto-created WhatsAppSession for profile %s", self.profile_id)
        except Exception as exc:
            logger.warning("Could not auto-create profile/session: %s", exc)

        # Replay any pending tasks that were queued while the extension was offline
        self._replay_pending_tasks()

    def disconnect(self, code):
        async_to_sync(self.channel_layer.group_discard)(self.group_name, self.channel_name)
        logger.info("Extension disconnected — profile %s", self.profile_id)

    # ── Messages FROM the extension ──────────────────────────────────────────

    def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            return

        t = data.get("type")
        # Log every incoming WS message so we can confirm messages arrive at Django
        if t == "incoming_messages":
            logger.info("[WS-Receive] profile=%s type=incoming_messages count=%d",
                        self.profile_id, len(data.get("messages", [])))
        elif t not in ("ping",):
            logger.info("[WS-Receive] profile=%s type=%s", self.profile_id, t)

        if t == "register":
            logger.info("Profile %s registered", self.profile_id)
        elif t == "ping":
            self.send(text_data=json.dumps({"type": "pong"}))
        elif t == "message_result":
            self._on_message_result(data)
        elif t == "message_send_status":
            self._on_message_send_status(data)
        elif t == "message_sent_ack":
            self._on_message_sent_ack(data)
        elif t == "incoming_messages":
            self._on_incoming(data)
        elif t == "agent_debug":
            logger.info(
                "Agent debug profile %s: %s %s",
                self.profile_id,
                data.get("message", ""),
                data.get("data", {}),
            )
        elif t == "status_report":
            logger.info("Profile %s WA status: %s", self.profile_id, data.get("wa_status"))

    def _on_message_result(self, data):
        from apps.campaigns.services.status import handle_message_result

        log_id = data.get("task_id")
        if not log_id:
            return

        handle_message_result(
            log_id=log_id,
            success=bool(data.get("success")),
            error=data.get("error", ""),
        )

    def _on_message_send_status(self, data):
        from apps.campaigns.services.status import mark_message_stage
        from apps.messaging.models import MessageLog

        log_id = data.get("message_id") or data.get("task_id")
        stage = data.get("stage") or data.get("status")
        status_map = {
            "extension_received": MessageLog.Status.EXTENSION_RECEIVED,
            "opening_chat": MessageLog.Status.OPENING_CHAT,
            "sending": MessageLog.Status.SENDING,
            "ack_received": MessageLog.Status.ACK_RECEIVED,
            "retrying": MessageLog.Status.RETRYING,
            "failed": MessageLog.Status.FAILED,
        }
        result = mark_message_stage(log_id, status_map.get(stage, stage), data.get("error", ""))
        logger.info(
            "Extension status received - profile=%s log=%s jid=%s stage=%s result=%s",
            self.profile_id,
            log_id,
            data.get("jid", ""),
            stage,
            result,
        )

    def _on_message_sent_ack(self, data):
        from apps.campaigns.services.status import handle_message_ack

        data["profile_id"] = data.get("profile_id") or self.profile_id
        result = handle_message_ack(data)
        logger.info(
            "ACK handled - profile=%s log=%s jid=%s status=%s result=%s",
            self.profile_id,
            data.get("message_id") or data.get("task_id"),
            data.get("jid", ""),
            data.get("status", ""),
            result,
        )

    def _on_incoming(self, data):
        from apps.autoreply.queues.incoming_queue import enqueue_incoming_message
        from apps.autoreply.services.message_filters import should_ignore_incoming_event
        from apps.sessions.models import WhatsAppSession

        messages = data.get("messages", [])
        logger.info(
            "[WS-Incoming] profile=%s  count=%d  senders=%s",
            self.profile_id,
            len(messages),
            [
                {
                    "name":    m.get("sender_name", ""),
                    "phone":   m.get("sender_phone", ""),
                    "jid":     m.get("jid", ""),
                    "preview": (m.get("preview") or "")[:40],
                    "source":  m.get("source", ""),
                    "count":   m.get("count", 1),
                }
                for m in messages
            ],
        )

        try:
            session = WhatsAppSession.objects.select_related("profile").get(
                profile__gologin_profile_id=self.profile_id
            )
        except WhatsAppSession.DoesNotExist:
            logger.warning("No WhatsAppSession for profile %s", self.profile_id)
            return

        for msg in messages:
            jid = msg.get("jid", "")
            if should_ignore_incoming_event(msg.get("sender_name", ""), msg.get("preview", "")):
                logger.debug(
                    "Incoming ignored - profile=%s jid=%s sender=%s preview=%s",
                    self.profile_id,
                    jid or "?",
                    msg.get("sender_name", ""),
                    (msg.get("preview") or "")[:40],
                )
                continue
            try:
                result = enqueue_incoming_message(session.id, msg)
                logger.info(
                    "Incoming queued - profile=%s jid=%s sender=%s result=%s",
                    self.profile_id,
                    jid or "?",
                    msg.get("sender_name", ""),
                    result,
                )
            except Exception as exc:
                logger.warning("Auto-reply queue error: %s", exc)

    # ── Messages TO the extension ────────────────────────────────────────────

    def push_task(self, event):
        """Called by channel-layer group_send from signals/tasks."""
        task = event["task"]
        logger.info(
            "WebSocket dispatch - profile=%s log=%s jid=%s conversation=%s",
            self.profile_id,
            task.get("message_id") or task.get("task_id"),
            task.get("jid", ""),
            task.get("conversation_id", ""),
        )
        try:
            self.send(text_data=json.dumps(task))
        except Exception as exc:
            logger.exception(
                "WebSocket emit failed - profile=%s log=%s jid=%s error=%s",
                self.profile_id,
                task.get("message_id") or task.get("task_id"),
                task.get("jid", ""),
                exc,
            )
            raise

    def _push_send(self, phone: str, message: str, log_id, jid: str = ""):
        jid = normalize_jid(jid, phone=phone)
        if not validate_jid(jid):
            logger.warning(
                "Rejecting push without valid jid - profile=%s phone=%s log=%s",
                self.profile_id,
                phone,
                log_id,
            )
            return
        task = {
            "type": "send_message_by_jid",
            "task_id": log_id,
            "message_id": log_id,
            "jid": jid,
            "message": message,
            "profile_id": self.profile_id,
        }
        self.send(text_data=json.dumps(task))

    def _replay_pending_tasks(self):
        """Push any PENDING MessageLogs to the extension after reconnect."""
        try:
            from apps.messaging.models import MessageLog
            from django.core.cache import cache
            pending = MessageLog.objects.filter(
                profile__gologin_profile_id=self.profile_id,
                status__in=[
                    MessageLog.Status.PENDING,
                    MessageLog.Status.SCHEDULED,
                    MessageLog.Status.DISPATCHED,
                    MessageLog.Status.EXTENSION_RECEIVED,
                    MessageLog.Status.OPENING_CHAT,
                    MessageLog.Status.SENDING,
                    MessageLog.Status.ACK_RECEIVED,
                    MessageLog.Status.RETRYING,
                ],
            ).order_by("id")[:50]

            count = 0
            for log in pending:
                jid = normalize_jid(log.whatsapp_jid, phone=log.phone_number)
                if not validate_jid(jid):
                    log.status = MessageLog.Status.SKIPPED
                    log.error_message = "Missing valid JID for replay"
                    log.save(update_fields=["status", "error_message"])
                    logger.warning(
                        "Pending task skipped - profile=%s log=%s missing_jid phone=%s",
                        self.profile_id,
                        log.id,
                        log.phone_number,
                    )
                    continue

                replay_lock_key = f"send_replay_lock:{self.profile_id}:{log.id}"
                if not cache.add(replay_lock_key, "1", timeout=180):
                    logger.info(
                        "[Dispatch] Replay skipped - profile=%s log=%s jid=%s reason=replay_lock",
                        self.profile_id,
                        log.id,
                        jid,
                    )
                    continue

                task = {
                    "type": "send_message_by_jid",
                    "task_id": log.id,
                    "message_id": log.id,
                    "jid": jid,
                    "message": log.message_body,
                    "profile_id": self.profile_id,
                }
                log.status = MessageLog.Status.DISPATCHED
                log.error_message = ""
                log.save(update_fields=["status", "error_message"])
                self.send(text_data=json.dumps(task))
                try:
                    from apps.campaigns.tasks import check_message_ack_timeout_task

                    check_message_ack_timeout_task.apply_async(args=[int(log.id)], countdown=90)
                except Exception as exc:
                    logger.warning(
                        "Could not schedule replay ACK timeout - profile=%s log=%s jid=%s error=%s",
                        self.profile_id,
                        log.id,
                        jid,
                        exc,
                    )
                count += 1

            if count:
                logger.info("[Dispatch] Replayed %d unresolved JID tasks for profile %s", count, self.profile_id)
        except Exception as exc:
            logger.warning("Could not replay pending tasks: %s", exc)


# ── Public helper used by signals & tasks ────────────────────────────────────

def push_task_to_profile(profile_id: str, phone: str, message: str,
                         log_id, jid: str = "", conversation_id=None) -> None:
    """
    Push a JID-routed send-message task to the connected Chrome extension.

    Phone is only used to derive a c.us JID when the caller has not supplied one.
    Name-based routing is intentionally unsupported here.
    """
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.warning("Channel layer not configured - cannot push task to extension")
        return

    jid = normalize_jid(jid, phone=phone)
    if not validate_jid(jid):
        logger.warning("Rejecting push without valid jid - profile=%s phone=%s log=%s", profile_id, phone, log_id)
        return

    task = {
        "type": "send_message_by_jid",
        "task_id": log_id,
        "message_id": log_id,
        "jid": jid,
        "message": message,
        "profile_id": profile_id,
        "conversation_id": conversation_id,
    }

    try:
        from apps.campaigns.services.status import mark_message_stage

        mark_message_stage(log_id, "dispatched")
    except Exception as exc:
        logger.warning("Could not mark dispatch - profile=%s log=%s jid=%s error=%s", profile_id, log_id, jid, exc)

    logger.info(
        "Task emitted - profile=%s jid=%s phone=%s log=%s conversation=%s",
        profile_id,
        jid,
        jid_to_phone(jid) or "?",
        log_id,
        conversation_id or "",
    )
    try:
        async_to_sync(channel_layer.group_send)(
            f"profile_{profile_id}",
            {"type": "push_task", "task": task},
        )
    except Exception as exc:
        logger.exception(
            "WebSocket group emit failed - profile=%s log=%s jid=%s error=%s",
            profile_id,
            log_id,
            jid,
            exc,
        )
        raise

    try:
        from apps.campaigns.tasks import check_message_ack_timeout_task

        check_message_ack_timeout_task.apply_async(args=[int(log_id)], countdown=90)
    except Exception as exc:
        logger.warning("Could not schedule ACK timeout - profile=%s log=%s jid=%s error=%s", profile_id, log_id, jid, exc)

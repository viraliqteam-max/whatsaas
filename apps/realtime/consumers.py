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
from django.utils import timezone
from apps.realtime.registry import (
    get_remote,
    register_remote,
    remove_remote,
    touch_remote,
    update_queue_depth,
)
from shared.services.websocket_events import emit_dashboard_event
from shared.utils.jid import jid_to_phone, normalize_jid, validate_jid

logger = logging.getLogger(__name__)


class DashboardConsumer(WebsocketConsumer):
    def connect(self):
        async_to_sync(self.channel_layer.group_add)("dashboard", self.channel_name)
        self.accept()
        logger.info("[WebSocket] dashboard connected")

    def disconnect(self, code):
        async_to_sync(self.channel_layer.group_discard)("dashboard", self.channel_name)
        logger.info("[WebSocket] dashboard disconnected code=%s", code)

    def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            return
        if data.get("type") == "ping":
            self.send(text_data=json.dumps({"type": "pong"}))

    def dashboard_event(self, event):
        self.send(text_data=json.dumps(event["event"]))


class AgentConsumer(WebsocketConsumer):

    def connect(self):
        self.profile_id = self.scope["url_route"]["kwargs"]["profile_id"]
        self.group_name = f"profile_{self.profile_id}"

        async_to_sync(self.channel_layer.group_add)(self.group_name, self.channel_name)
        self.accept()
        register_remote(self.profile_id, self.channel_name)
        logger.info("[WS-Connect] profile_id=%s channel=%s", self.profile_id, self.channel_name)

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
            profile.extension_connected = True
            profile.websocket_connected = True
            profile.browser_running = True
            profile.runtime_status = GoLoginProfile.RuntimeStatus.EXTENSION_CONNECTED
            profile.health_status = GoLoginProfile.HealthStatus.DEGRADED
            profile.last_heartbeat_at = timezone.now()
            if profile.sync_status == GoLoginProfile.SyncStatus.MISSING_REMOTE:
                profile.sync_status = GoLoginProfile.SyncStatus.SYNCED
            profile.save(update_fields=[
                "extension_connected", "websocket_connected", "browser_running",
                "runtime_status", "health_status", "last_heartbeat_at", "sync_status",
            ])
            try:
                from apps.profiles.runtime_registry import set_websocket_connected
                set_websocket_connected(self.profile_id, True)
            except Exception:
                pass
            emit_dashboard_event(
                "profile.reconnected",
                {
                    "profile_id": self.profile_id,
                    "runtime_status": profile.runtime_status,
                    "health_status": profile.health_status,
                },
            )

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

        logger.info("[WS-Connected] profile_id=%s", self.profile_id)

    def disconnect(self, code):
        async_to_sync(self.channel_layer.group_discard)(self.group_name, self.channel_name)
        remove_remote(self.profile_id, self.channel_name)
        logger.info("[WS-Disconnected] profile_id=%s code=%s", self.profile_id, code)
        try:
            from apps.profiles.runtime_registry import set_websocket_connected
            set_websocket_connected(self.profile_id, False)
        except Exception:
            pass
        try:
            from apps.profiles.models import GoLoginProfile

            remote = get_remote(self.profile_id)
            if remote and remote.get("alive"):
                return
            GoLoginProfile.objects.filter(gologin_profile_id=self.profile_id).update(
                extension_connected=False,
                websocket_connected=False,
                whatsapp_connected=False,
                runtime_status=GoLoginProfile.RuntimeStatus.RECONNECTING,
                health_status=GoLoginProfile.HealthStatus.UNHEALTHY,
            )
            emit_dashboard_event("profile.disconnected", {"profile_id": self.profile_id})
        except Exception as exc:
            logger.warning("[WebSocket] disconnect profile update failed profile=%s error=%s", self.profile_id, exc)

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
            register_remote(self.profile_id, self.channel_name, data)
            self._on_profile_heartbeat(data)
            logger.info("[WS-RegistryAdd] profile_id=%s websocket_id=%s", self.profile_id, data.get("websocket_id", ""))
        elif t == "ping":
            self.send(text_data=json.dumps({"type": "pong"}))
        elif t == "message_result":
            self._on_message_result(data)
        elif t == "message_send_status":
            self._on_message_send_status(data)
        elif t == "message_sent_ack":
            self._on_message_sent_ack(data)
        elif t == "message_failed_ack":
            self._on_message_sent_ack(data)
        elif t == "heartbeat":
            self._on_profile_heartbeat(data)
        elif t == "profile_heartbeat":
            self._on_profile_heartbeat(data)
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

    def _on_profile_heartbeat(self, data):
        from apps.profiles.services import mark_profile_heartbeat

        touch_remote(self.profile_id, self.channel_name, data)
        result = mark_profile_heartbeat(
            self.profile_id,
            whatsapp_ready=bool(data.get("whatsapp_ready")),
            active_chat=data.get("active_chat", ""),
            payload=data,
        )
        logger.info(
            "[WS-Heartbeat] received profile=%s whatsapp_ready=%s result=%s",
            self.profile_id,
            bool(data.get("whatsapp_ready")),
            result,
        )
        if data.get("whatsapp_ready"):
            from django.core.cache import cache

            if cache.add(f"replay_scan_lock:{self.profile_id}", "1", timeout=20):
                self._replay_pending_tasks()

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
            "[Extension-Receive] profile=%s log=%s jid=%s stage=%s result=%s",
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
            "[WS-ACK] profile=%s log=%s jid=%s status=%s result=%s",
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
            logger.info(
                "[ProfileValidated] profile=%s session=%s channel=%s",
                self.profile_id,
                session.id,
                self.channel_name,
            )
        except WhatsAppSession.DoesNotExist:
            logger.warning(
                "[ProfileValidated] failed reason=websocket_missing profile=%s channel=%s",
                self.profile_id,
                self.channel_name,
            )
            return

        for msg in messages:
            jid = msg.get("jid", "")
            sender = msg.get("sender_name", "")
            if should_ignore_incoming_event(sender, msg.get("preview", "")):
                logger.debug(
                    "[ProfileValidated] skipped reason=ignored profile=%s jid=%s sender=%s",
                    self.profile_id,
                    jid or "?",
                    sender,
                )
                continue
            # Cross-profile guard: the session's profile must match this WebSocket's profile_id.
            # This prevents a message extracted by profile A from being dispatched via profile B.
            session_profile_id = session.profile.gologin_profile_id
            if session_profile_id != self.profile_id:
                logger.error(
                    "[ProfileValidated] failed reason=profile_mismatch "
                    "ws_profile=%s session_profile=%s jid=%s sender=%s — dropping",
                    self.profile_id,
                    session_profile_id,
                    jid or "?",
                    sender,
                )
                continue
            try:
                result = enqueue_incoming_message(session.id, msg)
                logger.info(
                    "[ProfileValidated] queued profile=%s jid=%s sender=%s result=%s",
                    self.profile_id,
                    jid or "?",
                    sender,
                    result,
                )
            except Exception as exc:
                logger.warning(
                    "[ProfileValidated] queue_error profile=%s jid=%s sender=%s error=%s",
                    self.profile_id, jid or "?", sender, exc,
                )

    # ── Messages TO the extension ────────────────────────────────────────────

    def push_task(self, event):
        """Called by channel-layer group_send from signals/tasks."""
        task = event["task"]
        remote = get_remote(self.profile_id)
        if not remote or not remote.get("alive"):
            logger.warning(
                "[WS-SendTask] blocked profile=%s log=%s reason=%s",
                self.profile_id,
                task.get("message_id") or task.get("task_id"),
                (remote or {}).get("stale_reason") or "registry_missing",
            )
            return
        logger.info(
            "[WS-SendTask] profile=%s log=%s jid=%s conversation=%s",
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
        """Push stale PENDING/RETRYING MessageLogs to the extension after reconnect.

        Intentionally excludes DISPATCHED, EXTENSION_RECEIVED, OPENING_CHAT, SENDING,
        and ACK_RECEIVED — those are actively in-flight and handled exclusively by
        check_message_ack_timeout_task.  Replaying them here causes duplicate sends
        when the extension is mid-execution on the original task.

        Only replays messages older than 5 minutes to give the extension time to
        complete and ACK the current send before we consider a task truly stuck.
        """
        try:
            from apps.messaging.models import MessageLog
            from django.core.cache import cache
            from datetime import timedelta

            stale_cutoff = timezone.now() - timedelta(minutes=5)
            pending = MessageLog.objects.filter(
                profile__gologin_profile_id=self.profile_id,
                status__in=[
                    MessageLog.Status.PENDING,
                    MessageLog.Status.RETRYING,
                ],
                created_at__lt=stale_cutoff,
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
                logger.info("[Reconnect] replayed=%d profile=%s", count, self.profile_id)
            update_queue_depth(self.profile_id, queue_depth=max(len(pending) - count, 0), active_tasks=count)
        except Exception as exc:
            logger.warning("Could not replay pending tasks: %s", exc)


# ── Public helper used by signals & tasks ────────────────────────────────────

def push_task_to_profile(profile_id: str, phone: str, message: str,
                         log_id, jid: str = "", conversation_id=None) -> bool | None:
    """
    Push a JID-routed send-message task to the connected Chrome extension.

    Returns False when blocked because the profile is not ready (caller should
    stop dispatching further messages for this profile and defer).
    Returns None in all other cases (dispatched, duplicate-prevented, or error).

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

    try:
        from apps.campaigns.services.status import mark_message_stage
        from apps.profiles.services import profile_ready_for_dispatch
        from apps.profiles.tasks import ensure_profile_runtime_task
        from apps.campaigns.tasks import check_message_ack_timeout_task

        ready, reason = profile_ready_for_dispatch(profile_id)
        if not ready:
            mark_message_stage(log_id, "retrying", f"profile_not_ready:{reason}")
            # Rate-limit to ONE relaunch attempt per profile per 10 minutes.
            # Without this guard, every blocked message (potentially 100+) queues
            # its own ensure_profile_runtime_task, causing repeated GoLogin browser
            # relaunches every ~180 s (session_lock TTL) and an endless reconnect loop.
            from django.core.cache import cache as _rt_cache
            _ensure_key = f"ensure_runtime_scheduled:{profile_id}"
            if not _rt_cache.get(_ensure_key):
                _rt_cache.set(_ensure_key, "1", timeout=180)  # 3-min rate limit (was 10 min)
                ensure_profile_runtime_task.apply_async(args=[profile_id, f"dispatch:{reason}"], countdown=3)
            # Use a long countdown so the profile has time to reconnect before the
            # ACK-timeout task fires. 30 s burned through the 5-attempt budget in
            # 2.5 min; 120 s gives ~10 min for WhatsApp to come back online.
            check_message_ack_timeout_task.apply_async(args=[int(log_id)], countdown=120)
            logger.warning(
                "[Dispatch] blocked profile_id=%s jid=%s message_id=%s conversation_id=%s reason=%s",
                profile_id,
                jid,
                log_id,
                conversation_id or "",
                reason,
            )
            emit_dashboard_event(
                "campaign.status.updated",
                {
                    "profile_id": profile_id,
                    "message_id": log_id,
                    "jid": jid,
                    "status": "retrying",
                    "reason": reason,
                },
            )
            return False  # signal to caller that dispatch was blocked
    except Exception as exc:
        logger.warning("[CampaignDispatch] readiness check failed profile_id=%s log=%s jid=%s error=%s", profile_id, log_id, jid, exc)

    task = {
        "type": "send_message_by_jid",
        "task_id": log_id,
        "message_id": log_id,
        "jid": jid,
        "message": message,
        "profile_id": profile_id,
        "conversation_id": conversation_id,
    }

    # Dedup guard FIRST — must be acquired before mark_message_stage to prevent a
    # concurrent caller (e.g. _replay_pending_tasks racing with _fire_reply) from
    # marking the log DISPATCHED and then silently dropping the group_send.
    # TTL is 150 s — longer than the 90 s ACK-timeout countdown — so the guard is
    # still active when check_message_ack_timeout_task fires its first retry.
    from django.core.cache import cache as _cache

    dispatch_lock_key = f"send_dispatch_lock:{log_id}"
    if not _cache.add(dispatch_lock_key, "1", timeout=150):
        logger.info(
            "[DuplicatePrevented] profile=%s jid=%s log=%s — dispatch already in flight, skipping",
            profile_id, jid, log_id,
        )
        return

    try:
        from apps.campaigns.services.status import mark_message_stage

        mark_message_stage(log_id, "dispatched")
    except Exception as exc:
        logger.warning("Could not mark dispatch - profile=%s log=%s jid=%s error=%s", profile_id, log_id, jid, exc)

    logger.info(
        "[ProfileLockAcquired] profile=%s jid=%s log=%s — dispatch cleared, sending to extension",
        profile_id, jid, log_id,
    )
    logger.info(
        "[Dispatch] task_emitted profile=%s jid=%s phone=%s log=%s conversation=%s channel=%s",
        profile_id,
        jid,
        jid_to_phone(jid) or "?",
        log_id,
        conversation_id or "",
        f"profile_{profile_id}",
    )

    try:
        async_to_sync(channel_layer.group_send)(
            f"profile_{profile_id}",
            {"type": "push_task", "task": task},
        )
    except Exception as exc:
        _cache.delete(dispatch_lock_key)
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

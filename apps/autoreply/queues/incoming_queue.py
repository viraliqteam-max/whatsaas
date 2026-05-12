import logging
import threading

from celery.exceptions import CeleryError
from kombu.exceptions import KombuError
from shared.logging.events import log_event

logger = logging.getLogger(__name__)


def process_incoming_payload(session_id: int, payload: dict) -> str:
    from apps.autoreply.services.incoming import process_incoming_message
    from apps.sessions.models import WhatsAppSession

    session = WhatsAppSession.objects.select_related("profile").get(id=session_id)
    return process_incoming_message(
        session=session,
        sender_name=payload.get("sender_name", ""),
        sender_phone=payload.get("sender_phone", ""),
        jid=payload.get("jid", ""),
        preview=payload.get("preview", ""),
        count=payload.get("count", 1),
        # Stable identifier fields from enhanced extension payload
        serialized_id=payload.get("serialized_id", ""),
        pushname=payload.get("pushname", ""),
        chat_type=payload.get("chat_type", "private"),
        message_id=payload.get("message_id", ""),
        extraction_method=payload.get("extraction_method", ""),
        extraction_metadata={
            k: v for k, v in payload.items()
            if k in ("serialized_id", "pushname", "chat_type", "message_id",
                     "dataset_id", "extraction_method", "hydrated", "chat_opened",
                     "source", "count")
        },
    )


def enqueue_incoming_message(session_id: int, payload: dict) -> str:
    """
    Queue boundary for realtime incoming events.

    Celery is preferred so AI/reply work runs on incoming_queue. The thread
    fallback preserves local/dev behavior if the broker is temporarily down.
    """
    try:
        from apps.autoreply.tasks import process_incoming_message_task

        task = process_incoming_message_task.delay(session_id, dict(payload))
        log_event(
            logger,
            "incoming_enqueued",
            session_id=session_id,
            task_id=task.id,
            jid=payload.get("jid", "") or "?",
            sender=payload.get("sender_name", ""),
        )
        return "queued"
    except (CeleryError, KombuError, OSError) as exc:
        log_event(logger, "incoming_enqueue_fallback", logging.WARNING, session_id=session_id, error=exc)

    return enqueue_incoming_message_locally(session_id, payload)


def enqueue_incoming_message_locally(session_id: int, payload: dict) -> str:
    thread = threading.Thread(
        target=_process_incoming_payload_safely,
        args=(session_id, dict(payload)),
        daemon=True,
        name=f"incoming-{session_id}",
    )
    thread.start()
    return "queued"


def _process_incoming_payload_safely(session_id: int, payload: dict) -> None:
    try:
        result = process_incoming_payload(session_id, payload)
        log_event(
            logger,
            "incoming_processed",
            session_id=session_id,
            jid=payload.get("jid", "") or "?",
            sender=payload.get("sender_name", ""),
            result=result,
        )
    except Exception as exc:
        log_event(logger, "incoming_failed", logging.WARNING, session_id=session_id, error=exc)

from celery import shared_task

from apps.autoreply.queues.incoming_queue import process_incoming_payload
from shared.constants.queues import RETRY_QUEUE


@shared_task(bind=True, name="autoreply.process_incoming_message", max_retries=3)
def process_incoming_message_task(self, session_id: int, payload: dict):
    try:
        return process_incoming_payload(session_id, payload)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=15, queue=RETRY_QUEUE)

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def emit_dashboard_event(event_type: str, payload: dict, conversation_id=None) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    event = {
        "type": "dashboard_event",
        "event": {
            "type": event_type,
            "payload": payload,
        },
    }
    groups = ["dashboard"]
    if conversation_id:
        groups.append(f"conversation_{conversation_id}")

    for group in groups:
        try:
            async_to_sync(channel_layer.group_send)(group, event)
        except Exception as exc:
            logger.warning("Dashboard event failed group=%s type=%s error=%s", group, event_type, exc)

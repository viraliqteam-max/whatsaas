import logging
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

REGISTRY_TTL_SECONDS = 120
HEARTBEAT_STALE_SECONDS = 45
_PREFIX = "remote_registry"


def _key(profile_id: str) -> str:
    return f"{_PREFIX}:{profile_id}"


def _index_key() -> str:
    return f"{_PREFIX}:profiles"


def _lock_key(profile_id: str) -> str:
    return f"profile_lock:{profile_id}"


def _now():
    return timezone.now()


def _iso(dt):
    return dt.isoformat() if dt else None


def register_remote(profile_id: str, channel_name: str, payload: dict | None = None) -> dict:
    now = _now()
    previous = cache.get(_key(profile_id), {}) or {}
    if previous.get("channel_name") and previous.get("channel_name") != channel_name:
        logger.warning(
            "[WS-RegistryAdd] replacing_existing profile_id=%s old_channel=%s new_channel=%s",
            profile_id,
            previous.get("channel_name"),
            channel_name,
        )
    data = {
        "profile_id": profile_id,
        "channel_name": channel_name,
        "websocket_id": (payload or {}).get("websocket_id", ""),
        "browser_session": (payload or {}).get("browser_session", ""),
        "whatsapp_ready": bool((payload or {}).get("whatsapp_ready")),
        "status": "connected",
        "registered": True,
        "connected_at": _iso(now),
        "last_seen": _iso(now),
        "active_tasks": 0,
        "queue_depth": 0,
    }
    cache.set(_lock_key(profile_id), channel_name, timeout=REGISTRY_TTL_SECONDS)
    cache.set(_key(profile_id), data, timeout=REGISTRY_TTL_SECONDS)
    profiles = set(cache.get(_index_key(), []))
    profiles.add(profile_id)
    cache.set(_index_key(), sorted(profiles), timeout=None)
    logger.info("[WS-RegistryAdd] profile_id=%s websocket_id=%s", profile_id, data["websocket_id"])
    return data


def touch_remote(profile_id: str, channel_name: str = "", payload: dict | None = None) -> dict:
    now = _now()
    data = cache.get(_key(profile_id), {}) or {}
    data.update({
        "profile_id": profile_id,
        "status": "connected",
        "registered": True,
        "last_seen": _iso(now),
    })
    if channel_name:
        data["channel_name"] = channel_name
    if payload:
        if "websocket_id" in payload:
            data["websocket_id"] = payload.get("websocket_id") or data.get("websocket_id", "")
        if "browser_session" in payload:
            data["browser_session"] = payload.get("browser_session") or data.get("browser_session", "")
        if "whatsapp_ready" in payload:
            data["whatsapp_ready"] = bool(payload.get("whatsapp_ready"))
    cache.set(_lock_key(profile_id), data.get("channel_name", channel_name), timeout=REGISTRY_TTL_SECONDS)
    cache.set(_key(profile_id), data, timeout=REGISTRY_TTL_SECONDS)
    profiles = set(cache.get(_index_key(), []))
    profiles.add(profile_id)
    cache.set(_index_key(), sorted(profiles), timeout=None)
    logger.info("[WS-Heartbeat] profile_id=%s whatsapp_ready=%s", profile_id, data.get("whatsapp_ready"))
    return data


def remove_remote(profile_id: str, channel_name: str = "") -> None:
    data = cache.get(_key(profile_id), {}) or {}
    if channel_name and data.get("channel_name") and data.get("channel_name") != channel_name:
        logger.info("[WS-RegistryRemove] skipped profile_id=%s reason=newer_channel", profile_id)
        return
    cache.delete(_key(profile_id))
    cache.delete(_lock_key(profile_id))
    profiles = set(cache.get(_index_key(), []))
    profiles.discard(profile_id)
    cache.set(_index_key(), sorted(profiles), timeout=None)
    logger.info("[WS-RegistryRemove] profile_id=%s", profile_id)


def get_remote(profile_id: str) -> dict | None:
    data = cache.get(_key(profile_id))
    if not data:
        return None
    data = dict(data)
    data["alive"] = is_remote_alive(data)
    data["stale_reason"] = stale_reason(data)
    return data


def list_remotes() -> list[dict]:
    rows = []
    for profile_id in cache.get(_index_key(), []) or []:
        data = get_remote(profile_id)
        if data:
            rows.append(data)
    return rows


def stale_reason(data: dict | None) -> str:
    if not data:
        return "registry_missing"
    last_seen = data.get("last_seen")
    if not last_seen:
        return "heartbeat_missing"
    try:
        seen_at = datetime.fromisoformat(last_seen)
        if timezone.is_naive(seen_at):
            seen_at = timezone.make_aware(seen_at, timezone.get_current_timezone())
    except (TypeError, ValueError):
        return "heartbeat_invalid"
    if seen_at < _now() - timedelta(seconds=HEARTBEAT_STALE_SECONDS):
        return "stale_heartbeat"
    if not data.get("registered"):
        return "not_registered"
    return ""


def is_remote_alive(data: dict | None) -> bool:
    return stale_reason(data) == ""


def update_queue_depth(profile_id: str, queue_depth: int = 0, active_tasks: int = 0) -> None:
    data = cache.get(_key(profile_id), {}) or {}
    if not data:
        return
    data["queue_depth"] = queue_depth
    data["active_tasks"] = active_tasks
    cache.set(_key(profile_id), data, timeout=REGISTRY_TTL_SECONDS)

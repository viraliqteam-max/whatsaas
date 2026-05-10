import logging
from contextlib import contextmanager

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class SessionLockUnavailable(Exception):
    pass


def _lock_key(profile_id: str) -> str:
    return f"session-lock:{profile_id}"


@contextmanager
def session_lock(profile_id: str, owner: str, timeout: int = None, blocking: bool = False):
    """
    Lightweight per-profile lock for browser/session actions.

    This uses Django's cache backend. With the Redis cache configured in settings
    it works across Celery workers and the Django process; if Redis is replaced
    with a local cache, it still protects in-process actions but is weaker.
    """
    timeout = timeout or getattr(settings, "SESSION_ACTION_LOCK_TTL", 300)
    key = _lock_key(profile_id)
    token = f"{owner}:{profile_id}"
    acquired = cache.add(key, token, timeout=timeout)

    if not acquired:
        message = f"Session action already in progress for profile {profile_id}"
        if blocking:
            logger.warning("%s owner=%s", message, owner)
        raise SessionLockUnavailable(message)

    try:
        yield
    finally:
        cache.delete(key)

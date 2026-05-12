"""
In-process runtime registry — stores per-profile runtime state including
Playwright browser objects, Chrome PID, heartbeat timestamps, and session IDs.

This dict lives in the process that launched the browser (Django/Daphne or
a Celery worker). The Redis-backed realtime.registry is the cross-process
source of truth for WebSocket metadata (channel name, last_seen, etc.).

Public API:
    register(profile_id, runtime_session_id, ...)
    get(profile_id)            -> serializable dict (no Playwright objects)
    get_full(profile_id)       -> full dict including Playwright objects
    remove(profile_id)
    list_all()
    update_heartbeat(profile_id)
    set_websocket_connected(profile_id, connected)
    set_playwright_connected(profile_id, connected, browser, context, page)
    is_heartbeat_stale(profile_id, timeout_seconds)
    get_stale_profile_ids(timeout_seconds)
    validate_session(profile_id, runtime_session_id)
    is_pid_alive(profile_id)
    get_session_id(profile_id)
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# profile_id → {
#   profile_id, runtime_session_id, pid, debug_addr,
#   playwright_browser, playwright_context, playwright_page,
#   websocket_connected, playwright_connected,
#   last_heartbeat (monotonic float), registered_at (monotonic float),
# }
_RUNTIMES: dict[str, dict] = {}

_NON_SERIALIZABLE = frozenset({"playwright_browser", "playwright_context", "playwright_page"})


def _safe_copy(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in _NON_SERIALIZABLE}


def register(
    profile_id: str,
    runtime_session_id: str,
    pid: int | None = None,
    debug_addr: str = "",
    playwright_browser=None,
    playwright_context=None,
    playwright_page=None,
) -> dict:
    with _lock:
        entry = {
            "profile_id": profile_id,
            "runtime_session_id": str(runtime_session_id),
            "pid": pid,
            "debug_addr": debug_addr,
            "playwright_browser": playwright_browser,
            "playwright_context": playwright_context,
            "playwright_page": playwright_page,
            "websocket_connected": False,
            "playwright_connected": playwright_browser is not None,
            "last_heartbeat": time.monotonic(),
            "registered_at": time.monotonic(),
        }
        _RUNTIMES[profile_id] = entry
        logger.info(
            "[RuntimeRegistry] registered profile_id=%s session=%s pid=%s",
            profile_id, runtime_session_id, pid,
        )
        return _safe_copy(entry)


def get(profile_id: str) -> dict | None:
    """Return a serializable copy (no Playwright objects)."""
    with _lock:
        entry = _RUNTIMES.get(profile_id)
        return _safe_copy(entry) if entry else None


def get_full(profile_id: str) -> dict | None:
    """Return a full copy including Playwright objects (for internal lifecycle use)."""
    with _lock:
        entry = _RUNTIMES.get(profile_id)
        return dict(entry) if entry else None


def remove(profile_id: str) -> dict | None:
    with _lock:
        entry = _RUNTIMES.pop(profile_id, None)
        if entry:
            logger.info("[RuntimeRegistry] removed profile_id=%s", profile_id)
        return _safe_copy(entry) if entry else None


def list_all() -> dict:
    with _lock:
        return {pid: _safe_copy(entry) for pid, entry in _RUNTIMES.items()}


def update_heartbeat(profile_id: str) -> bool:
    with _lock:
        entry = _RUNTIMES.get(profile_id)
        if not entry:
            return False
        entry["last_heartbeat"] = time.monotonic()
        return True


def set_websocket_connected(profile_id: str, connected: bool) -> None:
    with _lock:
        entry = _RUNTIMES.get(profile_id)
        if entry:
            entry["websocket_connected"] = connected


def set_playwright_connected(
    profile_id: str,
    connected: bool,
    browser=None,
    context=None,
    page=None,
) -> None:
    with _lock:
        entry = _RUNTIMES.get(profile_id)
        if not entry:
            return
        entry["playwright_connected"] = connected
        if browser is not None:
            entry["playwright_browser"] = browser
        if context is not None:
            entry["playwright_context"] = context
        if page is not None:
            entry["playwright_page"] = page


def is_heartbeat_stale(profile_id: str, timeout_seconds: float = 15.0) -> bool:
    with _lock:
        entry = _RUNTIMES.get(profile_id)
        if not entry:
            return True
        return (time.monotonic() - entry["last_heartbeat"]) > timeout_seconds


def get_stale_profile_ids(timeout_seconds: float = 15.0) -> list[str]:
    """Return profile IDs whose last heartbeat is older than timeout_seconds."""
    stale = []
    now = time.monotonic()
    with _lock:
        for pid, entry in list(_RUNTIMES.items()):
            if (now - entry["last_heartbeat"]) > timeout_seconds:
                stale.append(pid)
    return stale


def validate_session(profile_id: str, runtime_session_id: str) -> bool:
    """Return True if the given session_id matches the registered runtime."""
    with _lock:
        entry = _RUNTIMES.get(profile_id)
    if not entry:
        return False
    return str(entry.get("runtime_session_id", "")) == str(runtime_session_id)


def is_pid_alive(profile_id: str) -> bool | None:
    """
    Check whether the Chrome PID is still running.
    Returns None if PID is not tracked (unknown state).
    """
    with _lock:
        entry = _RUNTIMES.get(profile_id)
    if not entry:
        return None
    pid = entry.get("pid")
    if not pid:
        return None
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        return psutil.Process(pid).status() not in (
            psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD
        )
    except Exception:
        return None


def get_session_id(profile_id: str) -> str | None:
    with _lock:
        entry = _RUNTIMES.get(profile_id)
    return str(entry["runtime_session_id"]) if entry else None

"""
Playwright-based WhatsApp Web inbox watcher.

Connects to the already-running GoLogin/Chrome browser via CDP and injects a
MutationObserver to detect new unread messages in real time.  Replaces the
Selenium poll_inbox() polling loop with an event-driven watcher.

Results feed directly into process_incoming_message() — same pipeline as the
extension's incoming_messages WebSocket event.

Public API (called by gologin_manager):
    wa_watcher.start(profile_id, debug_addr)
    wa_watcher.stop(profile_id)
    wa_watcher.is_watching(profile_id) -> bool
"""
import asyncio
import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 2.0          # minimum gap between consecutive DOM scans
_OBSERVER_REINJECT_INTERVAL = 30  # seconds between periodic observer re-injections

_watchers: dict[str, dict] = {}  # profile_id → {"thread": Thread, "stop": Event}
_registry_lock = threading.Lock()

# ── JavaScript injected into the WhatsApp Web page ────────────────────────────

# IIFE that installs a MutationObserver on document.body.
# Fires window.__wa_unread_changed__() whenever an unread-count badge
# is added, removed, or its data-testid attribute changes.
# The guard prevents double-installation on the same document.
_OBSERVER_JS = """
(function () {
    if (window.__wa_watcher_active__) return;
    window.__wa_watcher_active__ = true;

    const notify = () => { try { window.__wa_unread_changed__(); } catch (_) {} };

    new MutationObserver((mutations) => {
        for (const m of mutations) {
            for (const node of [...(m.addedNodes || []), ...(m.removedNodes || [])]) {
                if (node.nodeType !== 1) continue;
                if (
                    node.getAttribute?.('data-testid') === 'icon-unread-count' ||
                    node.querySelector?.('[data-testid="icon-unread-count"]')
                ) { notify(); return; }
            }
            if (
                m.type === 'attributes' &&
                m.target?.getAttribute?.('data-testid') === 'icon-unread-count'
            ) { notify(); return; }
        }
    }).observe(document.body, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: ['data-testid'],
    });
})();
"""

# Arrow function evaluated by page.evaluate() — Playwright calls it with no args
# and returns the JS return value as a Python list of dicts.
# Reads the same sidebar elements as poll_inbox() but in a single JS round-trip.
_SCAN_JS = """
() => {
    const seen = new Set();

    function getPreview(row) {
        const el = row.querySelector('[data-testid="last-msg-text"]')
                || row.querySelector('span.x1iyjqo2')
                || row.querySelector('[data-testid="last-msg-status"] span');
        return el ? el.textContent.trim() : '';
    }

    function getSenderName(row) {
        const el = row.querySelector('[data-testid="cell-frame-title"] span');
        return el ? el.textContent.trim() : '';
    }

    function pushRow(row, count) {
        const senderName = getSenderName(row);
        if (!senderName || senderName.length > 80) return;
        const key = senderName.toLowerCase();
        if (seen.has(key)) return;
        seen.add(key);
        out.push({
            sender_name:     senderName,
            message_preview: getPreview(row),
            unread_count:    count,
        });
    }

    const out = [];

    // Strategy 1: direct data-testid badge
    for (const badge of document.querySelectorAll(
        '[data-testid="icon-unread-count"], [data-testid="unread-count"], [data-testid="badge-count"]'
    )) {
        const row = badge.closest('[data-testid="cell-frame-container"]')
                 || badge.closest('[role="listitem"]');
        if (!row) continue;
        const count = parseInt(badge.textContent.trim()) || 1;
        pushRow(row, count);
    }

    // Strategy 2: aria-label "N unread" on the cell container
    for (const row of document.querySelectorAll(
        '[data-testid="cell-frame-container"], [role="listitem"]'
    )) {
        const label = row.getAttribute('aria-label') || '';
        const m = label.match(/(\\d+)\\s+unread/i);
        if (m) pushRow(row, parseInt(m[1]) || 1);
    }

    // Strategy 3: numeric span badge (1-999) when testid/aria-label changed
    if (out.length === 0) {
        for (const row of document.querySelectorAll('[data-testid="cell-frame-container"]')) {
            for (const sp of row.querySelectorAll('span')) {
                const t = (sp.textContent || '').trim();
                const n = parseInt(t, 10);
                if (!isNaN(n) && n > 0 && n <= 999 && String(n) === t) {
                    pushRow(row, n);
                    break;
                }
            }
        }
    }

    return out;
}
"""


# ── Public API ────────────────────────────────────────────────────────────────

def start(profile_id: str, debug_addr: str, on_disconnect=None) -> None:
    """
    Start an event-driven watcher thread for profile_id.  No-op if already running.

    on_disconnect: optional callable(profile_id) fired when the browser closes.
    Used by services.on_browser_disconnect to release the runtime lock and
    update DB/frontend state automatically.
    """
    with _registry_lock:
        if profile_id in _watchers:
            return
        stop_event = threading.Event()
        t = threading.Thread(
            target=_run,
            args=(profile_id, debug_addr, stop_event, on_disconnect),
            daemon=True,
            name=f"wa-watcher-{profile_id[:8]}",
        )
        _watchers[profile_id] = {"thread": t, "stop": stop_event}
    t.start()
    logger.info("WAWatcher started for profile %s → %s", profile_id, debug_addr)


def stop(profile_id: str) -> None:
    """Signal the watcher for profile_id to exit (non-blocking)."""
    with _registry_lock:
        entry = _watchers.pop(profile_id, None)
    if entry:
        entry["stop"].set()
        logger.info("WAWatcher stop requested for profile %s", profile_id)


def is_watching(profile_id: str) -> bool:
    """Return True if the watcher thread for profile_id is alive."""
    with _registry_lock:
        entry = _watchers.get(profile_id)
    return entry is not None and entry["thread"].is_alive()


# ── Private ────────────────────────────────────────────────────────────────────

def _find_wa_page(browser, timeout: float = 30.0):
    """
    Return the first WhatsApp Web page in the browser.

    Polls for up to `timeout` seconds because the watcher starts right after
    _trigger_ext_init(), which fires a fire-and-forget navigation.  The browser
    tab may still be on the ext-init URL (or mid-redirect) when we connect.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for context in browser.contexts:
            for page in context.pages:
                if "web.whatsapp.com" in page.url:
                    return page
        time.sleep(1)
    return None


def _inject_observer(profile_id: str, page) -> None:
    """Evaluate _OBSERVER_JS on the page — safe to call multiple times (guarded by JS flag)."""
    try:
        page.evaluate(_OBSERVER_JS)
    except Exception as exc:
        logger.debug("WAWatcher: observer inject failed for profile %s: %s", profile_id, exc)


def _run(profile_id: str, debug_addr: str, stop_event: threading.Event, on_disconnect=None) -> None:
    """
    Background thread body.

    Thread model
    ────────────
    Playwright's sync API has an internal asyncio event loop running in its own
    thread.  Our expose_function callback is invoked from that loop — so it must
    NOT call any synchronous Playwright API (deadlock risk).  We therefore only
    set a threading.Event from the callback; the DOM scan happens in this thread
    (the _run thread) after the event is signalled, where sync Playwright calls
    are safe.
    """
    # Windows + Python 3.12+: threads get SelectorEventLoop by default, which
    # doesn't support subprocess creation. Playwright needs ProactorEventLoop.
    if sys.platform == "win32":
        asyncio.set_event_loop(asyncio.ProactorEventLoop())

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "playwright not installed — run: pip install playwright && playwright install chromium"
        )
        with _registry_lock:
            _watchers.pop(profile_id, None)
        return

    scan_event = threading.Event()
    cdp_url = f"http://{debug_addr}"
    logger.info("WAWatcher connecting to %s for profile %s", cdp_url, profile_id)

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url)
            except Exception as exc:
                logger.error("WAWatcher CDP connect failed for profile %s: %s", profile_id, exc)
                return

            page = _find_wa_page(browser)
            if page is None:
                logger.warning("WAWatcher: no WhatsApp Web page found for profile %s", profile_id)
                return

            # expose_function registers window.__wa_unread_changed__ in the page's JS
            # context.  Playwright automatically re-exposes it after any navigation,
            # so one call here is enough for the lifetime of the page.
            try:
                page.expose_function("__wa_unread_changed__", lambda: scan_event.set())
            except Exception:
                # Already exposed (e.g. watcher was restarted without page reload).
                pass

            # For new WebSocket connections (e.g. WA reconnects after network drop)
            # use framereceived as a secondary trigger alongside the MutationObserver.
            def _on_ws(ws):
                if "web.whatsapp.com" not in ws.url:
                    return
                ws.on("framereceived", lambda _payload: scan_event.set())

            page.on("websocket", _on_ws)

            # Re-inject observer after any full page load (WA rarely reloads, but guard it).
            # expose_function survives reloads automatically; the MutationObserver does not.
            page.on("load", lambda: _inject_observer(profile_id, page))

            _inject_observer(profile_id, page)
            scan_event.set()  # trigger an immediate scan on startup

            last_scan = 0.0
            last_inject = time.monotonic()

            while not stop_event.is_set():
                triggered = scan_event.wait(timeout=5)

                if triggered:
                    scan_event.clear()
                    now = time.monotonic()
                    if now - last_scan >= _DEBOUNCE_SECONDS:
                        last_scan = now
                        try:
                            chats = page.evaluate(_SCAN_JS)
                            if chats:
                                _dispatch(profile_id, chats)
                        except Exception as exc:
                            logger.debug(
                                "WAWatcher scan error for profile %s: %s", profile_id, exc
                            )

                # Periodic re-injection handles the rare case of an unexpected reload
                # that clears window.__wa_watcher_active__ and loses the observer.
                now = time.monotonic()
                if now - last_inject > _OBSERVER_REINJECT_INTERVAL:
                    _inject_observer(profile_id, page)
                    last_inject = now

                if not browser.is_connected():
                    logger.warning(
                        "WAWatcher: browser disconnected for profile %s", profile_id
                    )
                    break

    except Exception as exc:
        logger.error("WAWatcher unexpected error for profile %s: %s", profile_id, exc)
    finally:
        # Fire disconnect callback so the runtime lock is released and DB/frontend updated
        if on_disconnect:
            try:
                on_disconnect(profile_id)
            except Exception as cb_exc:
                logger.warning("WAWatcher on_disconnect callback failed profile=%s: %s", profile_id, cb_exc)
        # Close the per-thread Django DB connection so it is not leaked.
        try:
            from django.db import connection as _db_conn
            _db_conn.close()
        except Exception:
            pass
        with _registry_lock:
            _watchers.pop(profile_id, None)
        logger.info("WAWatcher exited for profile %s", profile_id)


def _dispatch(profile_id: str, chats: list) -> None:
    """Feed a list of unread-chat dicts into the process_incoming_message() pipeline."""
    try:
        from apps.sessions.models import WhatsAppSession
        from apps.sessions.services import process_incoming_message

        session = WhatsAppSession.objects.select_related("profile").get(
            profile__gologin_profile_id=profile_id
        )
    except Exception as exc:
        logger.warning(
            "WAWatcher dispatch: no session for profile %s: %s", profile_id, exc
        )
        return

    for chat in chats:
        try:
            result = process_incoming_message(
                session=session,
                sender_name=chat.get("sender_name", ""),
                sender_phone=chat.get("sender_phone", ""),
                preview=chat.get("message_preview", ""),
                count=chat.get("unread_count", 1),
            )
            if result not in ("duplicate", "ignored", "old_message"):
                logger.info(
                    "WAWatcher → profile %s  sender=%s  result=%s",
                    profile_id,
                    chat.get("sender_name", ""),
                    result,
                )
        except Exception as exc:
            logger.warning("WAWatcher dispatch error: %s", exc)

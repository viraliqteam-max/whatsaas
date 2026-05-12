import logging
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone

from apps.profiles.models import GoLoginProfile
from apps.sessions.models import WhatsAppSession
from shared.services.websocket_events import emit_dashboard_event
from utils import gologin_manager as gl

logger = logging.getLogger(__name__)


class ProfileAlreadyLocked(Exception):
    pass


def _default_owner():
    return User.objects.filter(is_superuser=True).first() or User.objects.first()


# ── GoLogin sync ───────────────────────────────────────────────────────────────

def fetch_all_remote_profiles() -> list[dict]:
    profiles = []
    page = 1
    while True:
        data = gl.list_gologin_profiles(limit=50, page=page)
        batch = data.get("profiles", [])
        profiles.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return profiles


@transaction.atomic
def sync_gologin_profiles() -> dict:
    owner = _default_owner()
    if owner is None:
        logger.warning("[ProfileSync] skipped reason=no_owner")
        return {"status": "skipped", "reason": "no_owner"}

    now = timezone.now()
    try:
        remote_profiles = fetch_all_remote_profiles()
    except Exception as exc:
        GoLoginProfile.objects.exclude(gologin_profile_id__isnull=True).exclude(gologin_profile_id="").update(
            sync_status=GoLoginProfile.SyncStatus.SYNC_ERROR,
            last_synced_at=now,
        )
        logger.exception("[ProfileSync] failed error=%s", exc)
        return {"status": "failed", "error": str(exc)}

    remote_by_id = {p.get("id"): p for p in remote_profiles if p.get("id")}
    seen_ids = set(remote_by_id)
    created = renamed = synced = missing = 0

    for remote_id, remote in remote_by_id.items():
        name = remote.get("name") or f"GoLogin {remote_id}"
        os_type = remote.get("os") or GoLoginProfile.OSType.WINDOWS
        profile, was_created = GoLoginProfile.objects.get_or_create(
            gologin_profile_id=remote_id,
            defaults={
                "owner": owner,
                "name": name,
                "os_type": os_type,
                "sync_status": GoLoginProfile.SyncStatus.SYNCED,
                "last_synced_at": now,
            },
        )
        if was_created:
            created += 1
            emit_dashboard_event("profile.status.changed", {"profile_id": remote_id, "status": "created", "name": name})
            logger.info("[ProfileSync] created profile_id=%s name=%s", remote_id, name)
            continue

        updates = {"sync_status": GoLoginProfile.SyncStatus.SYNCED, "last_synced_at": now}
        if profile.name != name:
            updates["name"] = name
            renamed += 1
        if os_type and profile.os_type != os_type:
            updates["os_type"] = os_type
        if updates:
            GoLoginProfile.objects.filter(pk=profile.pk).update(**updates)
            synced += 1

    stale = (
        GoLoginProfile.objects
        .exclude(gologin_profile_id__isnull=True)
        .exclude(gologin_profile_id="")
        .exclude(gologin_profile_id__in=seen_ids)
    )
    for profile in stale:
        profile.sync_status = GoLoginProfile.SyncStatus.MISSING_REMOTE
        profile.status = GoLoginProfile.Status.INACTIVE
        profile.runtime_status = GoLoginProfile.RuntimeStatus.UNHEALTHY
        profile.health_status = GoLoginProfile.HealthStatus.UNHEALTHY
        profile.browser_running = False
        profile.extension_connected = False
        profile.whatsapp_connected = False
        profile.last_synced_at = now
        profile.save(update_fields=[
            "sync_status", "status", "runtime_status", "health_status",
            "browser_running", "extension_connected", "whatsapp_connected", "last_synced_at",
        ])
        try:
            from apps.messaging.models import Campaign
            paused = Campaign.objects.filter(profile=profile, status=Campaign.Status.RUNNING).update(
                status=Campaign.Status.PAUSED
            )
            if paused:
                emit_dashboard_event(
                    "campaign.status.updated",
                    {"profile_id": profile.gologin_profile_id, "status": "paused", "reason": "missing_remote", "count": paused},
                )
        except Exception as exc:
            logger.warning("[ProfileSync] campaign pause failed profile_id=%s error=%s", profile.gologin_profile_id, exc)
        missing += 1
        emit_dashboard_event(
            "profile.status.changed",
            {"profile_id": profile.gologin_profile_id, "status": "missing_remote", "name": profile.name},
        )
        logger.warning("[ProfileSync] missing_remote profile_id=%s name=%s", profile.gologin_profile_id, profile.name)

    result = {"status": "ok", "remote": len(remote_profiles), "created": created, "synced": synced, "renamed": renamed, "missing": missing}
    logger.info("[ProfileSync] complete %s", result)
    return result


# ── Runtime lock management ────────────────────────────────────────────────────

def acquire_runtime_lock(profile_id: str, owner: str = "api") -> tuple[uuid.UUID, GoLoginProfile]:
    """
    Atomically acquire exclusive runtime ownership of a profile.

    Uses SELECT FOR UPDATE (row-level DB lock) to prevent race conditions
    between concurrent launch attempts — e.g. a user double-clicking "Launch"
    or a Celery retry firing at the same time as an HTTP request.

    Returns (session_id, profile) on success.
    Raises ProfileAlreadyLocked if already locked.
    Raises GoLoginProfile.DoesNotExist if profile not found.
    """
    try:
        with transaction.atomic():
            try:
                profile = GoLoginProfile.objects.select_for_update(nowait=True).get(
                    gologin_profile_id=profile_id
                )
            except OperationalError:
                raise ProfileAlreadyLocked(f"Profile {profile_id} locked by a concurrent transaction")

            if profile.locked:
                raise ProfileAlreadyLocked(
                    f"Profile {profile_id} already locked by owner={profile.runtime_owner!r}"
                )

            session_id = uuid.uuid4()
            now = timezone.now()
            profile.locked = True
            profile.runtime_session_id = session_id
            profile.runtime_owner = owner
            profile.status = GoLoginProfile.Status.LAUNCHING
            profile.runtime_status = GoLoginProfile.RuntimeStatus.LAUNCHING
            profile.launched_at = now
            profile.last_launched_at = now
            profile.disconnect_reason = ""
            profile.browser_pid = None
            profile.playwright_connected = False
            profile.extension_connected = False
            profile.websocket_connected = False
            profile.whatsapp_connected = False
            profile.save(update_fields=[
                "locked", "runtime_session_id", "runtime_owner",
                "status", "runtime_status", "launched_at", "last_launched_at",
                "disconnect_reason", "browser_pid",
                "playwright_connected", "extension_connected",
                "websocket_connected", "whatsapp_connected",
            ])
            logger.info(
                "[RuntimeLock] acquired profile_id=%s session=%s owner=%s",
                profile_id, session_id, owner,
            )
            return session_id, profile
    except ProfileAlreadyLocked:
        raise
    except GoLoginProfile.DoesNotExist:
        raise


def release_runtime_lock(
    profile_id: str,
    session_id: str | uuid.UUID | None,
    reason: str = "stopped",
) -> bool:
    """
    Release the runtime lock and reset all runtime state.

    The session_id filter ensures that a stale disconnect callback cannot
    clobber a freshly started runtime with a different session_id.
    Pass session_id=None to force-release regardless of session (emergency only).

    Returns True if a row was actually updated.
    """
    filters: dict = {"gologin_profile_id": profile_id}
    if session_id is not None:
        filters["runtime_session_id"] = session_id

    updated = GoLoginProfile.objects.filter(**filters).update(
        locked=False,
        runtime_session_id=None,
        runtime_owner="",
        browser_pid=None,
        playwright_connected=False,
        extension_connected=False,
        websocket_connected=False,
        browser_running=False,
        whatsapp_connected=False,
        disconnect_reason=reason,
        runtime_status=GoLoginProfile.RuntimeStatus.DISCONNECTED,
        health_status=GoLoginProfile.HealthStatus.UNHEALTHY,
        status=GoLoginProfile.Status.INACTIVE,
    )
    if updated:
        logger.info(
            "[RuntimeLock] released profile_id=%s session=%s reason=%s",
            profile_id, session_id, reason,
        )
        return True
    logger.debug(
        "[RuntimeLock] no-op profile_id=%s session=%s reason=%s (mismatch or already unlocked)",
        profile_id, session_id, reason,
    )
    return False


# ── Browser disconnect cleanup ─────────────────────────────────────────────────

def on_browser_disconnect(profile_id: str) -> None:
    """
    Called when the Chrome browser closes or becomes unreachable.

    Triggered by:
    - wa_watcher: browser.is_connected() returns False
    - Playwright disconnect event (via wa_watcher)
    - cleanup_orphaned_locks: heartbeat timeout or dead PID
    """
    from apps.profiles.runtime_registry import get, remove

    entry = get(profile_id)
    session_id = (entry or {}).get("runtime_session_id")
    remove(profile_id)

    released = release_runtime_lock(profile_id, session_id, reason="browser_disconnect")

    try:
        from utils import wa_watcher
        wa_watcher.stop(profile_id)
    except Exception:
        pass

    try:
        from apps.messaging.models import Campaign
        paused = Campaign.objects.filter(
            profile__gologin_profile_id=profile_id,
            status=Campaign.Status.RUNNING,
        ).update(status=Campaign.Status.PAUSED)
        if paused:
            emit_dashboard_event(
                "campaign.status.updated",
                {"profile_id": profile_id, "status": "paused", "reason": "browser_disconnect", "count": paused},
            )
    except Exception as exc:
        logger.warning("[RuntimeCleanup] campaign pause failed profile_id=%s error=%s", profile_id, exc)

    if released:
        emit_dashboard_event(
            "profile.disconnected",
            {"profile_id": profile_id, "reason": "browser_disconnect", "runtime_status": "disconnected"},
        )
    logger.warning("[RuntimeCleanup] browser_disconnect profile_id=%s released=%s", profile_id, released)


# ── Full launch flow ───────────────────────────────────────────────────────────

def launch_profile_runtime(profile_id: str, owner: str = "api") -> dict:
    """
    Complete, lock-safe profile launch lifecycle:

    1. Acquire exclusive DB lock (SELECT FOR UPDATE) — blocks duplicates
    2. Emit launching event to frontend
    3. Launch GoLogin browser via SDK
    4. Register in in-process runtime registry (PID, debug_addr, session_id)
    5. wa_watcher attaches Playwright and fires on_browser_disconnect on death
    6. Update DB to browser_started with PID
    7. Return session metadata to caller
    """
    try:
        session_id, profile = acquire_runtime_lock(profile_id, owner=owner)
    except ProfileAlreadyLocked as exc:
        logger.info("[ProfileLaunch] blocked profile_id=%s reason=already_locked", profile_id)
        return {"status": "locked", "error": str(exc)}
    except GoLoginProfile.DoesNotExist:
        logger.warning("[ProfileLaunch] skipped profile_id=%s reason=missing_db_profile", profile_id)
        return {"status": "missing_profile"}

    emit_dashboard_event(
        "profile.status.changed",
        {
            "profile_id": profile_id,
            "runtime_status": GoLoginProfile.RuntimeStatus.LAUNCHING,
            "session_id": str(session_id),
        },
    )

    try:
        logger.info("[ProfileLaunch] starting browser profile_id=%s session=%s", profile_id, session_id)
        gl.launch_profile(
            profile_id,
            on_disconnect=lambda pid=profile_id: on_browser_disconnect(pid),
        )

        debug_addr = gl.get_debug_addr(profile_id)
        browser_pid = gl.get_browser_pid(profile_id)

        from apps.profiles.runtime_registry import register as _reg
        _reg(
            profile_id=profile_id,
            runtime_session_id=str(session_id),
            pid=browser_pid,
            debug_addr=debug_addr or "",
        )

        GoLoginProfile.objects.filter(
            gologin_profile_id=profile_id,
            runtime_session_id=session_id,
        ).update(
            browser_running=True,
            browser_pid=browser_pid,
            runtime_status=GoLoginProfile.RuntimeStatus.BROWSER_STARTED,
        )
        emit_dashboard_event(
            "profile.status.changed",
            {
                "profile_id": profile_id,
                "runtime_status": GoLoginProfile.RuntimeStatus.BROWSER_STARTED,
                "browser_pid": browser_pid,
                "session_id": str(session_id),
            },
        )
        logger.info(
            "[ProfileLaunch] browser_started profile_id=%s pid=%s debug_addr=%s session=%s",
            profile_id, browser_pid, debug_addr, session_id,
        )
        return {
            "status": "launched",
            "session_id": str(session_id),
            "browser_pid": browser_pid,
            "debug_addr": debug_addr,
        }

    except Exception as exc:
        logger.exception("[ProfileLaunch] failed profile_id=%s session=%s error=%s", profile_id, session_id, exc)
        release_runtime_lock(profile_id, session_id, reason="launch_failed")
        from apps.profiles.runtime_registry import remove as _reg_remove
        _reg_remove(profile_id)
        GoLoginProfile.objects.filter(gologin_profile_id=profile_id).update(
            runtime_status=GoLoginProfile.RuntimeStatus.CRASHED,
            health_status=GoLoginProfile.HealthStatus.UNHEALTHY,
            status=GoLoginProfile.Status.ERROR,
        )
        emit_dashboard_event(
            "profile.status.changed",
            {
                "profile_id": profile_id,
                "runtime_status": GoLoginProfile.RuntimeStatus.CRASHED,
                "error": str(exc),
                "session_id": str(session_id),
            },
        )
        return {"status": "failed", "error": str(exc)}


# ── Heartbeat management ───────────────────────────────────────────────────────

def mark_profile_heartbeat(
    profile_id: str,
    whatsapp_ready: bool,
    active_chat: str = "",
    payload: dict | None = None,
) -> str:
    from apps.profiles.runtime_registry import update_heartbeat, set_websocket_connected

    now = timezone.now()
    profile, _ = GoLoginProfile.objects.get_or_create(
        gologin_profile_id=profile_id,
        defaults={"owner": _default_owner(), "name": f"Profile {profile_id}"},
    )

    update_heartbeat(profile_id)
    set_websocket_connected(profile_id, True)

    profile.extension_connected = True
    profile.websocket_connected = True
    profile.whatsapp_connected = bool(whatsapp_ready)
    profile.browser_running = True
    profile.last_heartbeat_at = now
    if profile.sync_status == GoLoginProfile.SyncStatus.MISSING_REMOTE:
        profile.sync_status = GoLoginProfile.SyncStatus.SYNCED

    if whatsapp_ready:
        profile.runtime_status = GoLoginProfile.RuntimeStatus.ACTIVE
        profile.health_status = GoLoginProfile.HealthStatus.HEALTHY
    else:
        # Extension connected but WhatsApp not fully loaded yet
        profile.runtime_status = GoLoginProfile.RuntimeStatus.EXTENSION_CONNECTED
        profile.health_status = GoLoginProfile.HealthStatus.DEGRADED

    profile.status = GoLoginProfile.Status.ACTIVE
    profile.save(update_fields=[
        "extension_connected", "websocket_connected", "whatsapp_connected",
        "browser_running", "last_heartbeat_at", "sync_status",
        "runtime_status", "health_status", "status",
    ])

    session, _ = WhatsAppSession.objects.get_or_create(profile=profile)
    session.status = WhatsAppSession.Status.LOGGED_IN if whatsapp_ready else WhatsAppSession.Status.PENDING
    session.last_checked_at = now
    session.error_message = ""
    session.save(update_fields=["status", "last_checked_at", "error_message"])

    emit_dashboard_event(
        "profile.heartbeat",
        {
            "profile_id": profile_id,
            "whatsapp_ready": whatsapp_ready,
            "active_chat": active_chat,
            "runtime_status": profile.runtime_status,
            "health_status": profile.health_status,
            "timestamp": now.isoformat(),
            "payload": payload or {},
        },
    )
    logger.info(
        "[Heartbeat] profile_id=%s whatsapp_ready=%s active_chat=%s",
        profile_id, whatsapp_ready, active_chat,
    )
    return "updated"


def mark_stale_profiles_unhealthy(max_age_seconds: int = 15) -> int:
    """
    Mark profiles that have not sent a heartbeat within max_age_seconds as unhealthy.
    Default 15s = 3× the 5s extension heartbeat interval.
    """
    from apps.realtime.registry import get_remote, remove_remote

    cutoff = timezone.now() - timedelta(seconds=max_age_seconds)
    stale_qs = (
        GoLoginProfile.objects.filter(extension_connected=True).filter(
            last_heartbeat_at__lt=cutoff
        ) | GoLoginProfile.objects.filter(
            extension_connected=True, last_heartbeat_at__isnull=True
        )
    )
    count = 0
    for profile in stale_qs.distinct():
        remote = get_remote(profile.gologin_profile_id)
        if remote and remote.get("alive"):
            continue
        remove_remote(profile.gologin_profile_id)
        profile.extension_connected = False
        profile.websocket_connected = False
        profile.whatsapp_connected = False
        profile.health_status = GoLoginProfile.HealthStatus.UNHEALTHY
        profile.runtime_status = GoLoginProfile.RuntimeStatus.UNHEALTHY
        profile.save(update_fields=[
            "extension_connected", "websocket_connected", "whatsapp_connected",
            "health_status", "runtime_status",
        ])
        try:
            from apps.messaging.models import Campaign
            paused = Campaign.objects.filter(profile=profile, status=Campaign.Status.RUNNING).update(
                status=Campaign.Status.PAUSED
            )
            if paused:
                emit_dashboard_event(
                    "campaign.status.updated",
                    {"profile_id": profile.gologin_profile_id, "status": "paused", "reason": "heartbeat_stale", "count": paused},
                )
        except Exception as exc:
            logger.warning("[Heartbeat] campaign pause failed profile_id=%s error=%s", profile.gologin_profile_id, exc)
        emit_dashboard_event(
            "profile.disconnected",
            {"profile_id": profile.gologin_profile_id, "health_status": "unhealthy"},
        )
        logger.warning("[Heartbeat] stale profile_id=%s", profile.gologin_profile_id)
        count += 1
    return count


# ── Orphaned lock cleanup ──────────────────────────────────────────────────────

def cleanup_orphaned_locks(heartbeat_timeout_seconds: int = 15) -> dict:
    """
    Release phantom locks that outlived their runtime. Called every 10s by Celery beat.

    Three checks:
    1. In-process registry: entries with stale heartbeat + confirmed dead PID
    2. DB: locked profiles where browser never connected after 90s (launch timeout)
    3. DB: extension_connected=True but heartbeat stale (process-crossing fallback)
    """
    from apps.profiles.runtime_registry import (
        get_stale_profile_ids,
        remove as reg_remove,
        is_pid_alive,
    )

    now = timezone.now()
    stale_cutoff = now - timedelta(seconds=heartbeat_timeout_seconds)
    launch_timeout_cutoff = now - timedelta(seconds=90)
    released = 0

    # 1. In-process stale heartbeat + dead PID
    for pid_str in get_stale_profile_ids(timeout_seconds=float(heartbeat_timeout_seconds)):
        alive = is_pid_alive(pid_str)
        if alive is False:
            logger.warning("[OrphanCleanup] dead_pid profile_id=%s", pid_str)
            reg_remove(pid_str)
            if release_runtime_lock(pid_str, None, reason="pid_dead"):
                released += 1
                emit_dashboard_event(
                    "profile.disconnected",
                    {"profile_id": pid_str, "reason": "pid_dead"},
                )

    # 2. Locked profiles where extension never connected (launch timeout)
    for profile in GoLoginProfile.objects.filter(
        locked=True,
        extension_connected=False,
        launched_at__lt=launch_timeout_cutoff,
    ):
        pid = profile.browser_pid
        pid_alive = None
        if pid:
            try:
                import psutil
                pid_alive = psutil.pid_exists(pid)
            except Exception:
                pass
        if pid_alive is True:
            continue  # browser is up, just waiting for extension to connect

        logger.warning(
            "[OrphanCleanup] orphaned_lock profile_id=%s session=%s launched_at=%s",
            profile.gologin_profile_id, profile.runtime_session_id, profile.launched_at,
        )
        if release_runtime_lock(
            profile.gologin_profile_id, profile.runtime_session_id, reason="orphaned_lock"
        ):
            released += 1
            emit_dashboard_event(
                "profile.disconnected",
                {"profile_id": profile.gologin_profile_id, "reason": "orphaned_lock"},
            )

    # 3. DB-level heartbeat stale check (catches cross-process disconnects)
    for profile in GoLoginProfile.objects.filter(
        extension_connected=True,
        last_heartbeat_at__lt=stale_cutoff,
    ):
        try:
            from apps.realtime.registry import get_remote
            remote = get_remote(profile.gologin_profile_id)
            if remote and remote.get("alive"):
                continue
        except Exception:
            pass
        logger.warning("[OrphanCleanup] heartbeat_timeout profile_id=%s", profile.gologin_profile_id)
        if release_runtime_lock(profile.gologin_profile_id, None, reason="heartbeat_timeout"):
            released += 1
            emit_dashboard_event(
                "profile.disconnected",
                {"profile_id": profile.gologin_profile_id, "reason": "heartbeat_timeout"},
            )

    if released:
        logger.info("[OrphanCleanup] released=%d locks", released)
    return {"released": released}


# ── Best-profile selector ──────────────────────────────────────────────────────

def select_best_profile(user) -> "GoLoginProfile | None":
    """
    Return the best available GoLogin profile for automated campaign sending.

    Tier 1 — fully active: unlocked, whatsapp connected, healthy, heartbeat fresh (≤15s)
    Tier 2 — extension connected but WhatsApp may still be loading
    Tier 3 — any unlocked profile with a valid gologin_profile_id (will be launched on demand)
    Returns None only when the user has no usable profiles at all.
    """
    cutoff = timezone.now() - timedelta(seconds=15)

    from apps.messaging.models import Campaign

    base = (
        GoLoginProfile.objects
        .filter(owner=user, locked=False)
        .exclude(gologin_profile_id__isnull=True)
        .exclude(gologin_profile_id="")
        .exclude(campaigns__status=Campaign.Status.RUNNING)
    )

    tier1 = base.filter(
        whatsapp_connected=True,
        extension_connected=True,
        health_status=GoLoginProfile.HealthStatus.HEALTHY,
        runtime_status=GoLoginProfile.RuntimeStatus.ACTIVE,
        last_heartbeat_at__gte=cutoff,
    ).order_by("-last_heartbeat_at")
    if tier1.exists():
        return tier1.first()

    tier2 = base.filter(
        extension_connected=True,
        whatsapp_connected=True,
    ).order_by("-last_heartbeat_at")
    if tier2.exists():
        return tier2.first()

    return base.order_by("-last_launched_at").first()


# ── Backward-compatible wrappers ───────────────────────────────────────────────

def ensure_profile_runtime(profile_id: str, reason: str = "") -> dict:
    """Delegates to launch_profile_runtime; kept for call-site compatibility."""
    try:
        profile = GoLoginProfile.objects.get(gologin_profile_id=profile_id)
    except GoLoginProfile.DoesNotExist:
        logger.warning("[ProfileLaunch] skipped profile_id=%s reason=missing_db_profile", profile_id)
        return {"status": "missing_profile"}

    if profile.health_status == GoLoginProfile.HealthStatus.HEALTHY and profile.whatsapp_connected:
        return {"status": "ready"}

    return launch_profile_runtime(profile_id, owner=f"ensure:{reason or 'auto'}")


def profile_ready_for_dispatch(profile_id: str) -> tuple[bool, str]:
    from apps.realtime.registry import get_remote

    remote = get_remote(profile_id)
    try:
        profile = GoLoginProfile.objects.get(gologin_profile_id=profile_id)
    except GoLoginProfile.DoesNotExist:
        return False, "missing_profile:websocket_missing"
    if not remote:
        return False, "websocket_missing"
    if not remote.get("alive"):
        return False, remote.get("stale_reason") or "websocket_stale"
    if profile.sync_status == GoLoginProfile.SyncStatus.MISSING_REMOTE:
        profile.sync_status = GoLoginProfile.SyncStatus.SYNCED
        profile.save(update_fields=["sync_status"])
        logger.info("[WS-RegistryAdd] recovered_missing_remote profile_id=%s", profile_id)
    if not profile.extension_connected:
        return False, "extension_disconnected:db_flag_false"
    if not profile.whatsapp_connected:
        return False, "whatsapp_not_ready"
    if profile.health_status == GoLoginProfile.HealthStatus.UNHEALTHY:
        return False, "profile_unhealthy"
    if not profile.last_heartbeat_at:
        return False, "heartbeat_missing"
    if profile.last_heartbeat_at < timezone.now() - timedelta(seconds=15):
        return False, "heartbeat_stale"
    return True, "ready"

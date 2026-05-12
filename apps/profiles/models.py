import uuid

from django.contrib.auth.models import User
from django.db import models


class GoLoginProfile(models.Model):
    """Mirrors a GoLogin browser profile and tracks its local runtime state."""

    class Status(models.TextChoices):
        INACTIVE = "inactive", "Inactive"
        LAUNCHING = "launching", "Launching"
        ACTIVE = "active", "Active"
        ERROR = "error", "Error"

    class SyncStatus(models.TextChoices):
        LOCAL_ONLY = "local_only", "Local only"
        SYNCED = "synced", "Synced"
        MISSING_REMOTE = "missing_remote", "Missing in GoLogin"
        SYNC_ERROR = "sync_error", "Sync error"

    class RuntimeStatus(models.TextChoices):
        STOPPED = "stopped", "Stopped"
        LAUNCHING = "launching", "Launching"
        BROWSER_STARTED = "browser_started", "Browser Started"
        PLAYWRIGHT_CONNECTED = "playwright_connected", "Playwright Connected"
        EXTENSION_CONNECTED = "extension_connected", "Extension Connected"
        ACTIVE = "active", "Active"
        RECONNECTING = "reconnecting", "Reconnecting"
        DISCONNECTED = "disconnected", "Disconnected"
        CRASHED = "crashed", "Crashed"
        UNHEALTHY = "unhealthy", "Unhealthy"

    class HealthStatus(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        HEALTHY = "healthy", "Healthy"
        DEGRADED = "degraded", "Degraded"
        UNHEALTHY = "unhealthy", "Unhealthy"

    class OSType(models.TextChoices):
        WINDOWS = "win", "Windows"
        MAC = "mac", "macOS"
        LINUX = "lin", "Linux"

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="gologin_profiles")
    name = models.CharField(max_length=255)
    gologin_profile_id = models.CharField(
        max_length=255, unique=True, blank=True, null=True,
        help_text="ID from GoLogin API",
    )
    os_type = models.CharField(max_length=10, choices=OSType.choices, default=OSType.WINDOWS)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INACTIVE)
    sync_status = models.CharField(max_length=30, choices=SyncStatus.choices, default=SyncStatus.LOCAL_ONLY)
    runtime_status = models.CharField(
        max_length=30, choices=RuntimeStatus.choices, default=RuntimeStatus.STOPPED,
    )

    # ── Runtime orchestration ────────────────────────────────────────────────
    # One active runtime per profile. locked=True means a launch is in progress
    # or the browser is running. runtime_session_id is a per-launch UUID so that
    # cleanup from a stale session never clobbers a freshly started one.
    locked = models.BooleanField(default=False, db_index=True)
    runtime_session_id = models.UUIDField(null=True, blank=True, db_index=True)
    runtime_owner = models.CharField(max_length=255, blank=True, default="")
    browser_pid = models.IntegerField(null=True, blank=True)
    disconnect_reason = models.CharField(max_length=255, blank=True, default="")
    playwright_connected = models.BooleanField(default=False)

    # ── Runtime connection flags ─────────────────────────────────────────────
    extension_connected = models.BooleanField(default=False)
    websocket_connected = models.BooleanField(default=False)
    whatsapp_connected = models.BooleanField(default=False)
    browser_running = models.BooleanField(default=False)

    health_status = models.CharField(
        max_length=30, choices=HealthStatus.choices, default=HealthStatus.UNKNOWN,
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    # launched_at = current session start; last_launched_at = most recent launch ever
    launched_at = models.DateTimeField(null=True, blank=True)
    last_launched_at = models.DateTimeField(null=True, blank=True)

    # ── Profile metadata ─────────────────────────────────────────────────────
    notes = models.TextField(blank=True)
    business_context = models.TextField(
        blank=True,
        help_text=(
            "Describe your business so AI can reply in context. "
            "Example: 'We sell CRM software at $49/month. Demo available. "
            "Support: Mon-Sat 9am-6pm IST.'"
        ),
    )
    proxy = models.JSONField(default=dict, blank=True,
                             help_text='{"mode":"http","host":"","port":8080,"username":"","password":""}')
    extra_config = models.JSONField(default=dict, blank=True,
                                   help_text="Additional GoLogin profile configuration")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "GoLogin Profile"
        indexes = [
            models.Index(fields=["locked", "runtime_status"], name="profile_lock_rt_idx"),
            models.Index(fields=["extension_connected", "last_heartbeat_at"], name="profile_hb_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"

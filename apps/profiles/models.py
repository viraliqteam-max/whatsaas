from django.db import models
from django.contrib.auth.models import User


class GoLoginProfile(models.Model):
    """Mirrors a GoLogin browser profile and tracks its local state."""

    class Status(models.TextChoices):
        INACTIVE = "inactive", "Inactive"
        LAUNCHING = "launching", "Launching"
        ACTIVE = "active", "Active"
        ERROR = "error", "Error"

    class OSType(models.TextChoices):
        WINDOWS = "win", "Windows"
        MAC = "mac", "macOS"
        LINUX = "lin", "Linux"

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="gologin_profiles")
    name = models.CharField(max_length=255)
    gologin_profile_id = models.CharField(max_length=255, unique=True, blank=True, null=True,
                                          help_text="ID from GoLogin API")
    os_type = models.CharField(max_length=10, choices=OSType.choices, default=OSType.WINDOWS)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INACTIVE)
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
    last_launched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "GoLogin Profile"

    def __str__(self):
        return f"{self.name} ({self.status})"

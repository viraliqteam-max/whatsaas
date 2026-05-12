from rest_framework import serializers
from .models import GoLoginProfile


class GoLoginProfileSerializer(serializers.ModelSerializer):
    owner_username = serializers.CharField(source="owner.username", read_only=True)

    class Meta:
        model = GoLoginProfile
        fields = [
            "id", "owner", "owner_username", "name", "gologin_profile_id",
            "os_type", "status", "notes", "business_context", "proxy", "extra_config",
            "sync_status", "runtime_status", "extension_connected", "whatsapp_connected",
            "browser_running", "health_status", "last_synced_at", "last_heartbeat_at",
            "created_at", "updated_at", "last_launched_at",
        ]
        read_only_fields = [
            "id", "owner", "status", "sync_status", "runtime_status",
            "extension_connected", "whatsapp_connected", "browser_running", "health_status",
            "last_synced_at", "last_heartbeat_at", "created_at", "updated_at", "last_launched_at",
        ]


class GoLoginProfileCreateSerializer(serializers.ModelSerializer):
    """Used for creating profiles — optionally syncs with GoLogin API."""
    sync_with_gologin = serializers.BooleanField(
        default=True, write_only=True,
        help_text="If true, creates the profile on GoLogin and stores the remote ID"
    )

    class Meta:
        model = GoLoginProfile
        fields = [
            "name", "os_type", "notes", "business_context", "proxy", "extra_config", "sync_with_gologin",
        ]

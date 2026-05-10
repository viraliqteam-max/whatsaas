from rest_framework import serializers
from .models import MessageTemplate, Campaign, MessageLog
import logging

logger = logging.getLogger(__name__)


RUNTIME_STATUSES = [
    MessageLog.Status.PENDING,
    MessageLog.Status.SCHEDULED,
    MessageLog.Status.DISPATCHED,
    MessageLog.Status.EXTENSION_RECEIVED,
    MessageLog.Status.OPENING_CHAT,
    MessageLog.Status.SENDING,
    MessageLog.Status.ACK_RECEIVED,
    MessageLog.Status.RETRYING,
]


class MessageTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessageTemplate
        fields = ["id", "owner", "name", "body", "created_at", "updated_at"]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]


class CampaignSerializer(serializers.ModelSerializer):
    owner_username = serializers.CharField(source="owner.username", read_only=True)
    profile_name = serializers.CharField(source="profile.name", read_only=True)
    template_name = serializers.CharField(source="template.name", read_only=True)
    total_messages = serializers.SerializerMethodField()
    sent_count = serializers.SerializerMethodField()
    failed_count = serializers.SerializerMethodField()
    pending_count = serializers.SerializerMethodField()
    dispatched_count = serializers.SerializerMethodField()
    ack_received_count = serializers.SerializerMethodField()
    retrying_count = serializers.SerializerMethodField()
    runtime_count = serializers.SerializerMethodField()
    target_count = serializers.SerializerMethodField()

    class Meta:
        model = Campaign
        fields = [
            "id", "owner", "owner_username", "name",
            "profile", "profile_name",
            "template", "template_name", "custom_message",
            "target_contacts", "target_groups",
            "status", "auto_send",
            "min_delay_seconds", "max_delay_seconds",
            "campaign_timezone", "respect_time_windows", "allowed_time_windows",
            "scheduled_at", "started_at", "completed_at", "celery_task_id",
            "total_messages", "sent_count", "failed_count", "pending_count",
            "dispatched_count", "ack_received_count", "retrying_count",
            "runtime_count", "target_count",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "owner", "status", "started_at", "completed_at",
            "celery_task_id", "created_at", "updated_at",
        ]

    def get_total_messages(self, obj):
        try:
            total = obj.message_logs.count()
            if total:
                return total
            return self.get_target_count(obj)
        except Exception as exc:
            logger.exception("Campaign serializer total aggregation failed - campaign=%s error=%s", getattr(obj, "id", None), exc)
            return 0

    def to_representation(self, instance):
        try:
            from apps.campaigns.services.status import sync_campaign_runtime_status

            if sync_campaign_runtime_status(instance):
                instance.refresh_from_db(fields=["status", "completed_at"])
        except Exception as exc:
            logger.exception("Campaign serializer runtime status sync failed - campaign=%s error=%s", getattr(instance, "id", None), exc)
        return super().to_representation(instance)

    def get_sent_count(self, obj):
        try:
            return obj.message_logs.filter(status=MessageLog.Status.SENT).count()
        except Exception as exc:
            logger.exception("Campaign serializer sent aggregation failed - campaign=%s error=%s", getattr(obj, "id", None), exc)
            return 0

    def get_failed_count(self, obj):
        try:
            return obj.message_logs.filter(status=MessageLog.Status.FAILED).count()
        except Exception as exc:
            logger.exception("Campaign serializer failed aggregation failed - campaign=%s error=%s", getattr(obj, "id", None), exc)
            return 0

    def get_pending_count(self, obj):
        try:
            return obj.message_logs.filter(status__in=RUNTIME_STATUSES).count()
        except Exception as exc:
            logger.exception("Campaign serializer pending aggregation failed - campaign=%s error=%s", getattr(obj, "id", None), exc)
            return 0

    def get_dispatched_count(self, obj):
        try:
            return obj.message_logs.filter(
                status__in=[
                    MessageLog.Status.DISPATCHED,
                    MessageLog.Status.EXTENSION_RECEIVED,
                    MessageLog.Status.OPENING_CHAT,
                    MessageLog.Status.SENDING,
                    MessageLog.Status.ACK_RECEIVED,
                ]
            ).count()
        except Exception as exc:
            logger.exception("Campaign serializer dispatched aggregation failed - campaign=%s error=%s", getattr(obj, "id", None), exc)
            return 0

    def get_ack_received_count(self, obj):
        try:
            return obj.message_logs.filter(status=MessageLog.Status.ACK_RECEIVED).count()
        except Exception as exc:
            logger.exception("Campaign serializer ack aggregation failed - campaign=%s error=%s", getattr(obj, "id", None), exc)
            return 0

    def get_retrying_count(self, obj):
        try:
            return obj.message_logs.filter(status=MessageLog.Status.RETRYING).count()
        except Exception as exc:
            logger.exception("Campaign serializer retry aggregation failed - campaign=%s error=%s", getattr(obj, "id", None), exc)
            return 0

    def get_runtime_count(self, obj):
        return self.get_pending_count(obj)

    def get_target_count(self, obj):
        try:
            return obj.get_all_contacts().filter(is_active=True).count()
        except Exception as exc:
            logger.exception("Campaign serializer target aggregation failed - campaign=%s error=%s", getattr(obj, "id", None), exc)
            return 0

    def validate(self, data):
        if not data.get("template") and not data.get("custom_message"):
            raise serializers.ValidationError("Provide either a template or a custom_message.")
        return data


class MessageLogSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(source="contact.name", read_only=True, default="")
    contact_phone = serializers.CharField(source="contact.phone_number", read_only=True, default="")
    display_number = serializers.SerializerMethodField()

    class Meta:
        model = MessageLog
        fields = [
            "id", "campaign", "profile", "contact", "phone_number",
            "contact_name", "contact_phone", "display_number",
            "whatsapp_jid", "message_body", "status", "error_message",
            "ack_timeout_attempts", "sent_at", "created_at",
        ]
        read_only_fields = fields

    def get_display_number(self, obj):
        return obj.phone_number or obj.whatsapp_jid or getattr(obj.contact, "phone_number", "")


class SendDirectMessageSerializer(serializers.Serializer):
    """For sending a one-off message without a campaign."""
    profile_id = serializers.IntegerField(help_text="DB id of the GoLoginProfile to use")
    phone_number = serializers.CharField(help_text="International format without + (e.g. 12025550123)")
    message = serializers.CharField(help_text="Message text to send")

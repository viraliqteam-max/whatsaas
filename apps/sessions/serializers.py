from rest_framework import serializers
from .models import WhatsAppSession, IncomingMessage


class WhatsAppSessionSerializer(serializers.ModelSerializer):
    profile_name = serializers.CharField(source="profile.name", read_only=True)

    class Meta:
        model = WhatsAppSession
        fields = [
            "id", "profile", "profile_name", "status", "phone_number",
            "qr_code_base64", "last_checked_at", "logged_in_at",
            "error_message", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "status", "qr_code_base64", "last_checked_at",
            "logged_in_at", "error_message", "created_at", "updated_at",
        ]


class IncomingMessageSerializer(serializers.ModelSerializer):
    profile_name = serializers.CharField(source="session.profile.name", read_only=True)

    class Meta:
        model = IncomingMessage
        fields = [
            "id", "session", "profile_name", "sender_name", "sender_phone",
            "message_preview", "unread_count", "is_processed",
            "received_at", "created_at",
        ]
        read_only_fields = [
            "id", "session", "profile_name", "sender_name", "sender_phone",
            "message_preview", "unread_count", "received_at", "created_at",
        ]

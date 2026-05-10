from rest_framework import serializers

from apps.autoreply.models import (
    AIReplyLog,
    ConversationMessage,
    ConversationState,
    HandoffEvent,
    LeadProfile,
)
from apps.sessions.models import Conversation


class ConversationStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConversationState
        fields = [
            "stage",
            "detected_language",
            "last_intent",
            "last_question_key",
            "last_ai_reply_at",
            "human_active",
            "ai_paused_until",
            "metadata",
            "updated_at",
        ]


class LeadProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadProfile
        fields = [
            "id",
            "conversation",
            "company_name",
            "contact_name",
            "role",
            "business_type",
            "main_problem",
            "current_marketing_method",
            "website",
            "matched_service",
            "qualification_score",
            "is_qualified",
            "updated_at",
        ]
        read_only_fields = ["conversation", "qualification_score", "is_qualified", "updated_at"]


class ConversationMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConversationMessage
        fields = [
            "id",
            "conversation",
            "sender",
            "text",
            "intent",
            "language",
            "external_message_id",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields


class HandoffEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = HandoffEvent
        fields = ["id", "conversation", "reason", "requested_by", "active", "notes", "created_at", "resolved_at"]
        read_only_fields = fields


class AIReplyLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIReplyLog
        fields = ["id", "conversation", "user_message", "reply_text", "skipped_reason", "latency_ms", "created_at"]
        read_only_fields = fields


class ConversationSerializer(serializers.ModelSerializer):
    ai_state = ConversationStateSerializer(read_only=True)
    lead_profile = LeadProfileSerializer(read_only=True)

    class Meta:
        model = Conversation
        fields = [
            "id",
            "profile",
            "whatsapp_jid",
            "phone",
            "display_name",
            "ai_state",
            "lead_profile",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

from datetime import timedelta

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.autoreply.api.serializers import (
    ConversationMessageSerializer,
    ConversationSerializer,
    HandoffEventSerializer,
    LeadProfileSerializer,
)
from apps.autoreply.models import ConversationMessage, ConversationState, HandoffEvent, LeadProfile
from apps.autoreply.services.handoff import activate_handoff, resolve_handoff
from apps.sessions.models import Conversation
from shared.services.websocket_events import emit_dashboard_event


class ConversationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ConversationSerializer

    def get_queryset(self):
        return (
            Conversation.objects
            .filter(profile__owner=self.request.user)
            .select_related("profile", "ai_state", "lead_profile")
            .order_by("-updated_at")
        )

    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        conversation = self.get_object()
        qs = conversation.ai_messages.order_by("-created_at", "-id")[:100]
        rows = list(qs)
        rows.reverse()
        return Response(ConversationMessageSerializer(rows, many=True).data)

    @action(detail=True, methods=["post"])
    def pause_ai(self, request, pk=None):
        conversation = self.get_object()
        minutes = int(request.data.get("minutes", 60))
        state, _ = ConversationState.objects.get_or_create(conversation=conversation)
        state.ai_paused_until = timezone.now() + timedelta(minutes=minutes)
        state.save(update_fields=["ai_paused_until", "updated_at"])
        emit_dashboard_event("conversation.ai.paused", {"conversation_id": conversation.id}, conversation.id)
        return Response({"detail": "AI paused", "paused_until": state.ai_paused_until})

    @action(detail=True, methods=["post"])
    def resume_ai(self, request, pk=None):
        conversation = self.get_object()
        state, _ = ConversationState.objects.get_or_create(conversation=conversation)
        state.ai_paused_until = None
        state.human_active = False
        state.save(update_fields=["ai_paused_until", "human_active", "updated_at"])
        resolve_handoff(conversation)
        emit_dashboard_event("conversation.ai.resumed", {"conversation_id": conversation.id}, conversation.id)
        return Response({"detail": "AI resumed"})

    @action(detail=True, methods=["post"])
    def handoff(self, request, pk=None):
        conversation = self.get_object()
        reason = request.data.get("reason", "manual")
        event = activate_handoff(conversation, reason=reason, requested_by="human")
        emit_dashboard_event(
            "conversation.handoff.started",
            {"conversation_id": conversation.id, "reason": reason},
            conversation.id,
        )
        return Response(HandoffEventSerializer(event).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def human_message(self, request, pk=None):
        conversation = self.get_object()
        text = (request.data.get("text") or "").strip()
        if not text:
            return Response({"error": "text is required"}, status=status.HTTP_400_BAD_REQUEST)
        message = ConversationMessage.objects.create(
            conversation=conversation,
            sender=ConversationMessage.Sender.HUMAN,
            text=text,
        )
        emit_dashboard_event(
            "conversation.message.human",
            {"conversation_id": conversation.id, "message_id": message.id},
            conversation.id,
        )
        return Response(ConversationMessageSerializer(message).data, status=status.HTTP_201_CREATED)


class LeadProfileViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = LeadProfileSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        return LeadProfile.objects.filter(conversation__profile__owner=self.request.user).select_related("conversation")


class HandoffEventViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = HandoffEventSerializer

    def get_queryset(self):
        return HandoffEvent.objects.filter(conversation__profile__owner=self.request.user).select_related("conversation")

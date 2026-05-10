import logging
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.profiles.models import GoLoginProfile
from .models import WhatsAppSession, IncomingMessage
from .serializers import WhatsAppSessionSerializer, IncomingMessageSerializer
from utils import gologin_manager as gl
from utils import whatsapp_automation as wa

logger = logging.getLogger(__name__)


class WhatsAppSessionViewSet(viewsets.ModelViewSet):
    """
    WhatsApp Web session management per GoLogin profile.

    POST   /api/sessions/                        — create session record for a profile
    GET    /api/sessions/                        — list sessions
    GET    /api/sessions/{id}/                   — retrieve session
    POST   /api/sessions/{id}/check_status/      — check WhatsApp login status + return QR if needed
    POST   /api/sessions/{id}/screenshot/        — take a browser screenshot
    DELETE /api/sessions/{id}/                   — delete session record
    """

    permission_classes = [IsAuthenticated]
    serializer_class = WhatsAppSessionSerializer

    def get_queryset(self):
        return WhatsAppSession.objects.filter(profile__owner=self.request.user).select_related("profile")

    def _get_driver_or_error(self, profile: GoLoginProfile):
        driver = gl.get_active_driver(profile.gologin_profile_id)
        if driver is None:
            return None, Response(
                {"error": "Profile browser is not running. Launch the profile first via POST /api/profiles/{id}/launch/"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return driver, None

    # ------------------------------------------------------------------
    # Custom actions
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"])
    def check_status(self, request, pk=None):
        """
        Navigate to WhatsApp Web and return the session status.
        If QR code is needed, it is returned as base64 in the response.
        """
        session = self.get_object()
        driver, err = self._get_driver_or_error(session.profile)
        if err:
            return err

        result = wa.get_status(driver, navigate=True)

        session.status = result["status"]
        session.qr_code_base64 = result.get("qr_code_base64") or ""
        session.last_checked_at = timezone.now()
        if result["status"] == WhatsAppSession.Status.LOGGED_IN:
            session.logged_in_at = timezone.now()
        session.error_message = result.get("message", "") if result["status"] == "error" else ""
        session.save()

        serializer = self.get_serializer(session)
        return Response({**serializer.data, "message": result.get("message")})

    @action(detail=True, methods=["post"])
    def screenshot(self, request, pk=None):
        """Return a full-page screenshot of the current browser state."""
        session = self.get_object()
        driver, err = self._get_driver_or_error(session.profile)
        if err:
            return err

        b64 = wa.take_screenshot(driver)
        return Response({"screenshot_base64": b64, "url": driver.current_url})


class IncomingMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read incoming WhatsApp messages captured by the inbox poller.

    GET  /api/sessions/inbox/           — list all unread incoming messages
    GET  /api/sessions/inbox/{id}/      — retrieve a specific message
    POST /api/sessions/inbox/{id}/mark_processed/ — mark as handled

    Filter by ?is_processed=false to see only unread ones.
    Filter by ?session=<session_id> to see messages for one profile.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = IncomingMessageSerializer
    filterset_fields = ["is_processed", "session"]
    search_fields = ["sender_name", "sender_phone", "message_preview"]

    def get_queryset(self):
        return IncomingMessage.objects.filter(
            session__profile__owner=self.request.user
        ).select_related("session__profile")

    @action(detail=True, methods=["post"], url_path="mark_processed")
    def mark_processed(self, request, pk=None):
        """Mark this incoming message as processed (handled)."""
        msg = self.get_object()
        msg.is_processed = True
        msg.save(update_fields=["is_processed"])
        return Response({"detail": "Marked as processed", "id": msg.id})

    @action(detail=False, methods=["post"], url_path="mark_all_processed")
    def mark_all_processed(self, request):
        """Mark ALL unprocessed incoming messages as processed."""
        updated = self.get_queryset().filter(is_processed=False).update(is_processed=True)
        return Response({"detail": f"Marked {updated} messages as processed"})

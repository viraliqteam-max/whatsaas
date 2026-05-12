import logging
from django.utils import timezone
from django.db.models import Count, Q
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.profiles.models import GoLoginProfile
from apps.contacts.models import Contact
from .models import MessageTemplate, Campaign, MessageLog
from .serializers import (
    MessageTemplateSerializer,
    CampaignSerializer,
    MessageLogSerializer,
    SendDirectMessageSerializer,
)
from utils import gologin_manager as gl
from utils import whatsapp_automation as wa
from shared.utils.jid import normalize_jid
from apps.campaigns.services.status import sync_campaign_runtime_status

logger = logging.getLogger(__name__)


class MessageTemplateViewSet(viewsets.ModelViewSet):
    """
    Message template CRUD.

    POST   /api/messaging/templates/           — create template
    GET    /api/messaging/templates/           — list templates
    GET    /api/messaging/templates/{id}/      — retrieve template
    PUT    /api/messaging/templates/{id}/      — update template
    DELETE /api/messaging/templates/{id}/      — delete template
    POST   /api/messaging/templates/{id}/preview/ — preview rendered template
    """

    permission_classes = [IsAuthenticated]
    serializer_class = MessageTemplateSerializer

    def get_queryset(self):
        return MessageTemplate.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        """Render the template with sample context."""
        template = self.get_object()
        context = request.data.get("context", {"name": "John Doe", "phone_number": "12025550123"})
        rendered = template.render(context)
        return Response({"rendered": rendered, "context": context})


class CampaignViewSet(viewsets.ModelViewSet):
    """
    Campaign management — create, configure, launch, and monitor campaigns.

    POST   /api/messaging/campaigns/                       — create campaign
    GET    /api/messaging/campaigns/                       — list campaigns
    GET    /api/messaging/campaigns/{id}/                  — retrieve campaign
    PUT    /api/messaging/campaigns/{id}/                  — update campaign
    DELETE /api/messaging/campaigns/{id}/                  — delete campaign
    POST   /api/messaging/campaigns/{id}/start/            — launch campaign (async via Celery)
    POST   /api/messaging/campaigns/{id}/start_sync/       — launch campaign (synchronous, for testing)
    POST   /api/messaging/campaigns/{id}/pause/            — pause (stops scheduling new messages)
    GET    /api/messaging/campaigns/{id}/logs/             — get message logs for this campaign
    GET    /api/messaging/campaigns/{id}/stats/            — campaign stats summary
    GET    /api/messaging/campaigns/suggested_profile/     — best available profile for this user
    """

    permission_classes = [IsAuthenticated]
    serializer_class = CampaignSerializer

    def get_queryset(self):
        return Campaign.objects.filter(owner=self.request.user).select_related(
            "profile", "template"
        ).prefetch_related("target_contacts", "target_groups", "message_logs").annotate(
            logs_total=Count("message_logs", distinct=True),
            logs_sent=Count("message_logs", filter=Q(message_logs__status=MessageLog.Status.SENT), distinct=True),
            logs_failed=Count("message_logs", filter=Q(message_logs__status=MessageLog.Status.FAILED), distinct=True),
        ).order_by("-created_at", "-id")

    def list(self, request, *args, **kwargs):
        try:
            response = super().list(request, *args, **kwargs)
            count = len(response.data.get("results", response.data)) if response.data is not None else 0
            logger.info("Dashboard campaigns query executed - user=%s count=%s", request.user.id, count)
            return response
        except Exception as exc:
            logger.exception("Dashboard campaigns query failed - user=%s error=%s", request.user.id, exc)
            raise

    def perform_create(self, serializer):
        from apps.profiles.services import select_best_profile
        from django.utils import timezone as tz

        # Auto-select the best available profile when not provided
        profile = serializer.validated_data.get("profile")
        if not profile:
            profile = select_best_profile(self.request.user)

        # Auto-generate campaign name when not provided
        name = serializer.validated_data.get("name", "").strip()
        if not name:
            intent = serializer.validated_data.get("campaign_intent", "outreach")
            date_label = tz.localdate().strftime("%d %b")
            name = f"{intent.title()} — {date_label}"

        ai_mode = serializer.validated_data.get("ai_mode", True)
        serializer.save(owner=self.request.user, profile=profile, name=name, ai_mode=ai_mode)

    @action(detail=False, methods=["get"], url_path="suggested_profile")
    def suggested_profile(self, request):
        """Return the best available profile for automated campaign sending."""
        from apps.profiles.services import select_best_profile
        profile = select_best_profile(request.user)
        if not profile:
            return Response(
                {"profile": None, "message": "No usable profiles found. Launch a profile first."},
                status=status.HTTP_200_OK,
            )
        return Response({
            "profile": {
                "id": profile.id,
                "name": profile.name,
                "gologin_profile_id": profile.gologin_profile_id,
                "runtime_status": profile.runtime_status,
                "health_status": profile.health_status,
                "whatsapp_connected": profile.whatsapp_connected,
            },
            "message": f"Auto-selected: {profile.name}",
        })

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        """Dispatch the campaign to a Celery worker (non-blocking)."""
        from apps.campaigns.queues.campaign_queue import enqueue_campaign

        campaign = self.get_object()
        sync_campaign_runtime_status(campaign)
        campaign.refresh_from_db(fields=["status"])
        active_logs = campaign.message_logs.filter(
            status__in=[
                MessageLog.Status.PENDING,
                MessageLog.Status.SCHEDULED,
                MessageLog.Status.DISPATCHED,
                MessageLog.Status.EXTENSION_RECEIVED,
                MessageLog.Status.OPENING_CHAT,
                MessageLog.Status.SENDING,
                MessageLog.Status.ACK_RECEIVED,
                MessageLog.Status.RETRYING,
            ]
        ).exists()
        if campaign.status == Campaign.Status.RUNNING and active_logs:
            return Response({"error": "Campaign is already running"}, status=status.HTTP_400_BAD_REQUEST)
        if campaign.status == Campaign.Status.RUNNING and not active_logs:
            logger.warning("Dashboard start repaired stuck running campaign with no active logs - campaign=%s", campaign.id)

        task = enqueue_campaign(campaign.id)
        campaign.status = Campaign.Status.SCHEDULED
        campaign.celery_task_id = task.id
        campaign.save(update_fields=["status", "celery_task_id"])

        return Response({
            "detail": "Campaign dispatched to Celery",
            "task_id": task.id,
            "campaign_id": campaign.id,
        })

    @action(detail=True, methods=["post"], url_path="start_sync")
    def start_sync(self, request, pk=None):
        """
        Run the campaign synchronously in the request/response cycle.
        Useful for local testing without a Celery worker running.
        WARNING: will block the request until all messages are sent.
        NOTE: time-window enforcement is skipped here so tests run immediately.
        """
        import time
        import random
        campaign = self.get_object()
        sync_campaign_runtime_status(campaign)
        campaign.refresh_from_db(fields=["status"])
        active_logs = campaign.message_logs.filter(
            status__in=[
                MessageLog.Status.PENDING,
                MessageLog.Status.SCHEDULED,
                MessageLog.Status.DISPATCHED,
                MessageLog.Status.EXTENSION_RECEIVED,
                MessageLog.Status.OPENING_CHAT,
                MessageLog.Status.SENDING,
                MessageLog.Status.ACK_RECEIVED,
                MessageLog.Status.RETRYING,
            ]
        ).exists()
        if campaign.status == Campaign.Status.RUNNING and active_logs:
            return Response({"error": "Campaign is already running"}, status=status.HTTP_400_BAD_REQUEST)
        if campaign.status == Campaign.Status.RUNNING and not active_logs:
            logger.warning("Dashboard realtime run repaired stuck running campaign with no active logs - campaign=%s", campaign.id)

        profile = campaign.profile
        if not profile:
            return Response({"error": "Campaign has no GoLogin profile"}, status=status.HTTP_400_BAD_REQUEST)

        driver = gl.get_active_driver(profile.gologin_profile_id)
        if not driver:
            return Response(
                {"error": "Profile browser not active. Launch it first via POST /api/profiles/{id}/launch/"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        campaign.status = Campaign.Status.RUNNING
        campaign.started_at = timezone.now()
        campaign.save(update_fields=["status", "started_at"])

        contacts = list(campaign.get_all_contacts().filter(is_active=True))
        sent, failed = 0, 0
        min_wait = max(campaign.min_delay_seconds, 1)
        max_wait = max(campaign.max_delay_seconds, min_wait)

        for index, contact in enumerate(contacts):
            message_body = campaign.get_message_for(contact)
            log = MessageLog.objects.create(
                campaign=campaign,
                profile=profile,
                contact=contact,
                phone_number=contact.phone_number,
                whatsapp_jid=normalize_jid(getattr(contact, "whatsapp_jid", ""), phone=contact.phone_number),
                message_body=message_body,
                status=MessageLog.Status.PENDING,
            )

            result = wa.send_message(driver, contact.phone_number, message_body)

            if result["success"]:
                log.status = MessageLog.Status.SENT
                log.sent_at = timezone.now()
                sent += 1
            else:
                log.status = MessageLog.Status.FAILED
                log.error_message = result.get("error", "")
                failed += 1

            log.save(update_fields=["status", "sent_at", "error_message"])
            if index < len(contacts) - 1:
                time.sleep(random.randint(min_wait, max_wait))

        campaign.status = Campaign.Status.COMPLETED
        campaign.completed_at = timezone.now()
        campaign.save(update_fields=["status", "completed_at"])

        return Response({
            "detail": "Campaign completed",
            "sent": sent,
            "failed": failed,
            "total": len(contacts),
        })

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        """
        Single "Run" button — push all campaign messages to the Chrome extension.

        POST /api/messaging/campaigns/{id}/run/

        Requirements (zero manual setup needed):
          • Chrome extension must be connected to this Django server via WebSocket.
          • WhatsApp Web must be open and logged in inside that browser.
          • No Selenium driver or profile launch required.

        The extension processes messages one at a time (serial queue) and reports
        results back in real-time.  Campaign status updates automatically.
        """
        return self._push_to_extension(request, pk)

    @action(detail=True, methods=["post"], url_path="run_realtime")
    def run_realtime(self, request, pk=None):
        """Alias for /run/ — kept for backwards compatibility."""
        return self._push_to_extension(request, pk)

    def _push_to_extension(self, request, pk):
        """Push all pending campaign messages to the connected Chrome extension."""
        from apps.realtime.consumers import push_task_to_profile
        from utils.ai_message import generate_personalized_message

        campaign = self.get_object()
        if campaign.status == Campaign.Status.RUNNING:
            return Response({"error": "Campaign is already running"}, status=status.HTTP_400_BAD_REQUEST)

        profile = campaign.profile
        if not profile or not profile.gologin_profile_id:
            return Response({"error": "Campaign has no GoLogin profile"}, status=status.HTTP_400_BAD_REQUEST)

        contacts = list(campaign.get_all_contacts().filter(is_active=True))
        if not contacts:
            return Response({"error": "No active contacts in this campaign"}, status=status.HTTP_400_BAD_REQUEST)

        campaign.status = Campaign.Status.RUNNING
        campaign.started_at = timezone.now()
        campaign.save(update_fields=["status", "started_at"])

        dispatched, failed_dispatch = 0, 0
        for contact in contacts:
            template_body = campaign.template.body if campaign.template else campaign.custom_message
            message_body = generate_personalized_message(
                template_body=template_body or "",
                contact_name=contact.name,
                contact_phone=contact.phone_number,
            )
            log = MessageLog.objects.create(
                campaign=campaign,
                profile=profile,
                contact=contact,
                phone_number=contact.phone_number,
                whatsapp_jid=normalize_jid(getattr(contact, "whatsapp_jid", ""), phone=contact.phone_number),
                message_body=message_body,
                status=MessageLog.Status.PENDING,
            )
            try:
                push_task_to_profile(
                    profile.gologin_profile_id,
                    contact.phone_number,
                    message_body,
                    log.id,
                    jid=log.whatsapp_jid,
                )
                dispatched += 1
            except Exception as exc:
                log.status = MessageLog.Status.FAILED
                log.error_message = str(exc)
                log.save(update_fields=["status", "error_message"])
                failed_dispatch += 1
                logger.warning("WebSocket push failed for %s: %s", contact.phone_number, exc)

        return Response({
            "detail": f"Dispatched {dispatched} messages to extension. Results update in real-time.",
            "dispatched": dispatched,
            "failed_dispatch": failed_dispatch,
            "total": len(contacts),
        })

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        campaign = self.get_object()
        if campaign.status != Campaign.Status.RUNNING:
            return Response({"error": "Only running campaigns can be paused"}, status=status.HTTP_400_BAD_REQUEST)
        campaign.status = Campaign.Status.PAUSED
        campaign.save(update_fields=["status"])
        return Response({"detail": "Campaign paused (in-flight messages may still complete)"})

    @action(detail=True, methods=["get"])
    def logs(self, request, pk=None):
        try:
            campaign = self.get_object()
            sync_campaign_runtime_status(campaign)
            logs = campaign.message_logs.select_related("contact", "profile", "campaign").order_by("-created_at")
            logger.info(
                "Dashboard campaign logs query executed - user=%s campaign=%s count=%s",
                request.user.id,
                campaign.id,
                logs.count(),
            )
            page = self.paginate_queryset(logs)
            if page is not None:
                serializer = MessageLogSerializer(page, many=True)
                return self.get_paginated_response(serializer.data)
            serializer = MessageLogSerializer(logs, many=True)
            return Response(serializer.data)
        except Exception as exc:
            logger.exception("Dashboard campaign logs query failed - user=%s campaign=%s error=%s", request.user.id, pk, exc)
            raise

    @action(detail=True, methods=["get"])
    def stats(self, request, pk=None):
        try:
            campaign = self.get_object()
            sync_campaign_runtime_status(campaign)
            campaign.refresh_from_db(fields=["status", "completed_at"])
            logs = campaign.message_logs
            total_logs = logs.count()
            target_count = campaign.get_all_contacts().filter(is_active=True).count()
            total = total_logs or target_count
            sent = logs.filter(status=MessageLog.Status.SENT).count()
            failed = logs.filter(status=MessageLog.Status.FAILED).count()
            pending = logs.filter(
                status__in=[
                    MessageLog.Status.PENDING,
                    MessageLog.Status.SCHEDULED,
                    MessageLog.Status.DISPATCHED,
                    MessageLog.Status.EXTENSION_RECEIVED,
                    MessageLog.Status.OPENING_CHAT,
                    MessageLog.Status.SENDING,
                    MessageLog.Status.ACK_RECEIVED,
                    MessageLog.Status.RETRYING,
                ]
            ).count()
            dispatched = logs.filter(
                status__in=[
                    MessageLog.Status.DISPATCHED,
                    MessageLog.Status.EXTENSION_RECEIVED,
                    MessageLog.Status.OPENING_CHAT,
                    MessageLog.Status.SENDING,
                    MessageLog.Status.ACK_RECEIVED,
                ]
            ).count()
            retrying = logs.filter(status=MessageLog.Status.RETRYING).count()
            payload = {
                "campaign_id": campaign.id,
                "campaign_name": campaign.name,
                "status": campaign.status,
                "total": total,
                "total_logs": total_logs,
                "target_count": target_count,
                "sent": sent,
                "failed": failed,
                "pending": pending,
                "dispatched": dispatched,
                "retrying": retrying,
                "delivery_rate": round(sent / total * 100, 2) if total else 0,
            }
            logger.info("Dashboard stats query executed - user=%s campaign=%s payload=%s", request.user.id, campaign.id, payload)
            return Response(payload)
        except Exception as exc:
            logger.exception("Dashboard stats query failed - user=%s campaign=%s error=%s", request.user.id, pk, exc)
            raise


class MessageLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only message log history.

    GET  /api/messaging/logs/        — list all logs for this user
    GET  /api/messaging/logs/{id}/   — retrieve specific log
    """

    permission_classes = [IsAuthenticated]
    serializer_class = MessageLogSerializer
    filterset_fields = ["status", "campaign"]
    search_fields = ["phone_number", "message_body"]

    def get_queryset(self):
        try:
            qs = MessageLog.objects.filter(
                profile__owner=self.request.user
            ).select_related("campaign", "profile", "contact")
            logger.info("Dashboard all logs query prepared - user=%s", self.request.user.id)
            return qs
        except Exception as exc:
            logger.exception("Dashboard all logs query failed - user=%s error=%s", self.request.user.id, exc)
            raise


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_direct_message(request):
    """
    POST /api/messaging/send/
    Send a single WhatsApp message via the connected Chrome extension (WebSocket).
    No Selenium or profile launch required — extension must be connected.
    """
    serializer = SendDirectMessageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    profile_db_id = serializer.validated_data["profile_id"]
    phone = serializer.validated_data["phone_number"]
    message = serializer.validated_data["message"]

    try:
        profile = GoLoginProfile.objects.get(id=profile_db_id, owner=request.user)
    except GoLoginProfile.DoesNotExist:
        return Response({"error": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    if not profile.gologin_profile_id:
        return Response(
            {"error": "Profile has no AdsPower profile ID configured."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    log = MessageLog.objects.create(
        profile=profile,
        phone_number=phone,
        whatsapp_jid=normalize_jid("", phone=phone),
        message_body=message,
        status=MessageLog.Status.PENDING,
    )

    try:
        from apps.realtime.consumers import push_task_to_profile
        push_task_to_profile(profile.gologin_profile_id, phone, message, log.id, jid=log.whatsapp_jid)
        success = True
        error = None
    except Exception as exc:
        log.status = MessageLog.Status.FAILED
        log.error_message = str(exc)
        log.save(update_fields=["status", "error_message"])
        success = False
        error = str(exc)

    return Response({
        "success": success,
        "log_id": log.id,
        "error": error,
    }, status=status.HTTP_200_OK if success else status.HTTP_500_INTERNAL_SERVER_ERROR)

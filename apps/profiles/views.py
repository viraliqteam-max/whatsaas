import logging
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from .models import GoLoginProfile
from .serializers import GoLoginProfileSerializer, GoLoginProfileCreateSerializer
from utils import gologin_manager as gl

logger = logging.getLogger(__name__)


class GoLoginProfileViewSet(viewsets.ModelViewSet):
    """
    CRUD + launch/stop for GoLogin browser profiles.

    POST   /api/profiles/                  — create profile
    GET    /api/profiles/                  — list your profiles
    GET    /api/profiles/{id}/             — retrieve profile
    PUT    /api/profiles/{id}/             — update profile
    DELETE /api/profiles/{id}/             — delete profile (+ GoLogin remote)
    POST   /api/profiles/{id}/launch/      — launch browser
    POST   /api/profiles/{id}/stop/        — stop browser
    GET    /api/profiles/{id}/status/      — driver pool status
    GET    /api/profiles/active/           — list all active profiles
    GET    /api/profiles/gologin_list/     — fetch profiles from GoLogin API
    """

    permission_classes = [IsAuthenticated]
    serializer_class = GoLoginProfileSerializer

    def get_queryset(self):
        return GoLoginProfile.objects.filter(owner=self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return GoLoginProfileCreateSerializer
        return GoLoginProfileSerializer

    def perform_create(self, serializer):
        sync = self.request.data.get("sync_with_gologin", True)
        name = serializer.validated_data["name"]
        os_type = serializer.validated_data.get("os_type", "win")

        gologin_profile_id = None
        if sync:
            try:
                remote = gl.create_gologin_profile(name=name, os_type=os_type)
                gologin_profile_id = remote.get("id")
            except Exception as exc:
                logger.warning("GoLogin API create failed (saving locally only): %s", exc)

        serializer.save(owner=self.request.user, gologin_profile_id=gologin_profile_id)

    def perform_destroy(self, instance):
        if instance.gologin_profile_id:
            try:
                gl.delete_gologin_profile(instance.gologin_profile_id)
            except Exception as exc:
                logger.warning("GoLogin API delete failed: %s", exc)
        instance.delete()

    # ------------------------------------------------------------------
    # Custom actions
    # ------------------------------------------------------------------

    @action(detail=True, methods=["post"])
    def launch(self, request, pk=None):
        """Start the GoLogin profile and attach a Selenium driver."""
        profile = self.get_object()

        if not profile.gologin_profile_id:
            return Response(
                {"error": "This profile has no GoLogin ID. Create it via GoLogin first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        profile.status = GoLoginProfile.Status.LAUNCHING
        profile.save(update_fields=["status"])

        try:
            gl.launch_profile(profile.gologin_profile_id)
            profile.status = GoLoginProfile.Status.ACTIVE
            profile.last_launched_at = timezone.now()
            profile.save(update_fields=["status", "last_launched_at"])
            return Response({"detail": "Profile launched", "profile_id": profile.gologin_profile_id})
        except Exception as exc:
            profile.status = GoLoginProfile.Status.ERROR
            profile.save(update_fields=["status"])
            logger.error("Failed to launch profile %s: %s", profile.gologin_profile_id, exc)
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["post"])
    def stop(self, request, pk=None):
        """Stop the GoLogin profile and release the Selenium driver."""
        profile = self.get_object()

        if not profile.gologin_profile_id:
            return Response({"error": "No GoLogin ID"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            gl.stop_profile(profile.gologin_profile_id)
            profile.status = GoLoginProfile.Status.INACTIVE
            profile.save(update_fields=["status"])
            return Response({"detail": "Profile stopped"})
        except Exception as exc:
            logger.error("Failed to stop profile %s: %s", profile.gologin_profile_id, exc)
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["get"])
    def driver_status(self, request, pk=None):
        """Return whether the driver is alive in the in-process pool."""
        profile = self.get_object()
        is_active = profile.gologin_profile_id in gl.list_active_profiles()
        return Response({"driver_active": is_active, "db_status": profile.status})

    @action(detail=False, methods=["get"])
    def active(self, request):
        """List profile IDs currently in the driver pool."""
        return Response({"active_profile_ids": gl.list_active_profiles()})

    @action(detail=False, methods=["get"])
    def gologin_list(self, request):
        """Proxy to GoLogin API — returns raw profile list from GoLogin."""
        try:
            data = gl.list_gologin_profiles()
            return Response(data)
        except Exception as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=False, methods=["get"], url_path="detect", permission_classes=[AllowAny])
    def detect(self, request):
        """
        Called by the Chrome extension on startup to auto-detect its GoLogin profile
        by matching the browser's spoofed User-Agent against GoLogin profile data.
        No authentication required — called from the extension service worker.
        """
        ua = request.META.get("HTTP_USER_AGENT", "").strip()
        if not ua:
            return Response({"error": "No User-Agent header"}, status=status.HTTP_400_BAD_REQUEST)

        profile_ids = list(
            GoLoginProfile.objects
            .exclude(gologin_profile_id__isnull=True)
            .exclude(gologin_profile_id="")
            .values_list("gologin_profile_id", flat=True)
        )

        for pid in profile_ids:
            try:
                full = gl.get_gologin_profile(pid)
                nav_ua = full.get("navigator", {}).get("userAgent", "")
                if nav_ua and nav_ua.lower() == ua.lower():
                    logger.info("detect: matched profile %s by User-Agent", pid)
                    return Response({"profile_id": pid})
            except Exception as exc:
                logger.warning("detect: could not fetch profile %s: %s", pid, exc)
                continue

        return Response({"error": "No matching profile found"}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=["post"], url_path="auto_create")
    def auto_create(self, request):
        """
        Auto-create N new GoLogin profiles without touching any existing ones.

        POST /api/profiles/auto_create/
        Body: { "count": 5, "name_prefix": "WA Profile" }

        Steps:
          1. Fetch all existing GoLogin profile IDs from the cloud API.
          2. Create N brand-new profiles via POST /browser/quick.
          3. Verify each returned ID is not in the existing set (safety guard).
          4. Save each as a GoLoginProfile in the local database.
        """
        count = int(request.data.get("count", 5))
        if count < 1 or count > 20:
            return Response(
                {"error": "count must be between 1 and 20"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        name_prefix = str(request.data.get("name_prefix", "WA Profile")).strip() or "WA Profile"

        # Snapshot existing GoLogin IDs so we never overwrite them
        try:
            remote_data = gl.list_gologin_profiles(limit=200)
            existing_ids = {p["id"] for p in remote_data.get("profiles", [])}
            logger.info("auto_create: found %d existing GoLogin profiles", len(existing_ids))
        except Exception as exc:
            logger.warning("auto_create: could not fetch existing profiles: %s", exc)
            existing_ids = set()

        # Also collect IDs already saved in local DB so we don't duplicate
        local_ids = set(
            GoLoginProfile.objects.filter(owner=request.user)
            .exclude(gologin_profile_id__isnull=True)
            .values_list("gologin_profile_id", flat=True)
        )
        all_known_ids = existing_ids | local_ids

        created_profiles = []
        errors = []

        for i in range(1, count + 1):
            name = f"{name_prefix} {i}"
            try:
                remote = gl.create_gologin_profile(name=name, os_type="win")
                new_id = remote.get("id")

                if not new_id:
                    errors.append(f"Slot {i} ({name}): GoLogin returned no profile ID")
                    continue

                if new_id in all_known_ids:
                    errors.append(
                        f"Slot {i} ({name}): GoLogin returned existing ID {new_id} — skipped"
                    )
                    continue

                db_profile = GoLoginProfile.objects.create(
                    owner=request.user,
                    name=name,
                    gologin_profile_id=new_id,
                    os_type=GoLoginProfile.OSType.WINDOWS,
                )
                all_known_ids.add(new_id)
                created_profiles.append({
                    "db_id": db_profile.id,
                    "name": db_profile.name,
                    "gologin_profile_id": new_id,
                })
                logger.info("auto_create: created profile %s (%s)", name, new_id)
            except Exception as exc:
                errors.append(f"Slot {i} ({name}): {exc}")
                logger.warning("auto_create error for slot %d: %s", i, exc)

        response_status = (
            status.HTTP_201_CREATED if created_profiles else status.HTTP_400_BAD_REQUEST
        )
        return Response(
            {
                "created": len(created_profiles),
                "profiles": created_profiles,
                "errors": errors,
            },
            status=response_status,
        )

import os
from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, include
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView


def frontend_view(request):
    """Serve the single-page frontend app."""
    html_path = os.path.join(settings.BASE_DIR, "frontend", "index.html")
    with open(html_path, encoding="utf-8") as f:
        response = HttpResponse(f.read(), content_type="text/html")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


def ext_init_view(request, profile_id):
    """
    Extension auto-configuration page.

    GoLogin opens this URL when the profile starts (set as startUrl during profile creation).
    Two mechanisms ensure the extension picks up the profile_id:
      1. init.js content script fires at document_start and sends SET_PROFILE_ID to the SW.
      2. A persistent cookie 'gl_profile_id' is set so the SW can read it via chrome.cookies
         even after the extension is reloaded (without re-visiting this page).
    """
    response = HttpResponse(
        f"<!DOCTYPE html><html><head><title>Connecting…</title>"
        f"<style>body{{font-family:sans-serif;display:flex;align-items:center;"
        f"justify-content:center;height:100vh;margin:0;background:#f0f2f5}}"
        f"p{{color:#555;font-size:15px}}</style></head>"
        f"<body><p>Configuring extension for profile <b>{profile_id}</b>…<br>"
        f"Redirecting to WhatsApp Web.</p></body></html>",
        content_type="text/html",
    )
    response.set_cookie(
        'gl_profile_id', profile_id,
        max_age=365 * 24 * 3600,
        path='/',
        samesite='Lax',
        httponly=False,
    )
    return response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def system_config(request):
    """GET /api/system/config/ — current system configuration status."""
    from django.conf import settings
    secret = getattr(settings, "WEBHOOK_SECRET_KEY", "change-this-secret")
    host = request.build_absolute_uri("/").rstrip("/")
    webhook_url = f"{host}/api/contacts/webhook/?secret={secret}"
    return Response({
        "claude_ai": bool(getattr(settings, "OPENAI_API_KEY", "")),
        "webhook_url": webhook_url,
        "webhook_secret": secret,
        "channel_layer": "InMemory (no Redis needed)",
    })


urlpatterns = [
    # Frontend — served directly from Django (no separate HTTP server needed)
    path("", frontend_view, name="frontend"),
    path("favicon.ico", lambda r: HttpResponse(status=204)),

    # Extension auto-configuration — GoLogin opens this on profile start
    path("ext-init/<str:profile_id>/", ext_init_view, name="ext-init"),

    path("admin/", admin.site.urls),

    # Auth
    path("api/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),

    # System config status
    path("api/system/config/", system_config, name="system-config"),

    # App routes
    path("api/profiles/", include("apps.profiles.urls")),
    path("api/sessions/", include("apps.sessions.urls")),
    path("api/messaging/", include("apps.messaging.urls")),
    path("api/contacts/", include("apps.contacts.urls")),
    path("api/autoreply/", include("apps.autoreply.api.urls")),

    # API Docs
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

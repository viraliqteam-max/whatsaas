from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    MessageTemplateViewSet,
    CampaignViewSet,
    MessageLogViewSet,
    send_direct_message,
)

router = DefaultRouter()
router.register(r"templates", MessageTemplateViewSet, basename="templates")
router.register(r"campaigns", CampaignViewSet, basename="campaigns")
router.register(r"logs", MessageLogViewSet, basename="message-logs")

urlpatterns = [
    path("send/", send_direct_message, name="send-direct-message"),
    path("", include(router.urls)),
]

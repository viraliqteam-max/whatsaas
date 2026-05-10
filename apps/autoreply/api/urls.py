from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.autoreply.api.views import ConversationViewSet, HandoffEventViewSet, LeadProfileViewSet

router = DefaultRouter()
router.register(r"conversations", ConversationViewSet, basename="autoreply-conversations")
router.register(r"leads", LeadProfileViewSet, basename="autoreply-leads")
router.register(r"handoffs", HandoffEventViewSet, basename="autoreply-handoffs")

urlpatterns = [
    path("", include(router.urls)),
]

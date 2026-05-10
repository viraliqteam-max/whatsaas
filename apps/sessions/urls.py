from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import WhatsAppSessionViewSet, IncomingMessageViewSet

router = DefaultRouter()
router.register(r"inbox", IncomingMessageViewSet, basename="inbox")
router.register(r"", WhatsAppSessionViewSet, basename="sessions")

urlpatterns = [
    path("", include(router.urls)),
]

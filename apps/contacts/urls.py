from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ContactViewSet, ContactGroupViewSet, webhook_contact

router = DefaultRouter()
router.register(r"groups", ContactGroupViewSet, basename="contact-groups")
router.register(r"", ContactViewSet, basename="contacts")

urlpatterns = [
    path("webhook/", webhook_contact, name="contact-webhook"),
    path("", include(router.urls)),
]

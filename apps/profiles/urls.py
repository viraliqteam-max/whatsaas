from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import GoLoginProfileViewSet

router = DefaultRouter()
router.register(r"", GoLoginProfileViewSet, basename="profiles")

urlpatterns = [
    path("", include(router.urls)),
]

from django.contrib import admin
from .models import GoLoginProfile


@admin.register(GoLoginProfile)
class GoLoginProfileAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "gologin_profile_id", "os_type", "status", "last_launched_at"]
    list_filter = ["status", "os_type"]
    search_fields = ["name", "gologin_profile_id"]
    readonly_fields = ["created_at", "updated_at", "last_launched_at"]

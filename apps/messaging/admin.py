from django.contrib import admin
from .models import MessageTemplate, Campaign, MessageLog


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "created_at"]
    search_fields = ["name", "body"]


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "profile", "status", "started_at", "completed_at"]
    list_filter = ["status"]
    search_fields = ["name"]
    filter_horizontal = ["target_contacts", "target_groups"]
    readonly_fields = ["started_at", "completed_at", "celery_task_id", "created_at", "updated_at"]


@admin.register(MessageLog)
class MessageLogAdmin(admin.ModelAdmin):
    list_display = ["phone_number", "status", "campaign", "profile", "sent_at", "created_at"]
    list_filter = ["status"]
    search_fields = ["phone_number", "message_body"]
    readonly_fields = ["created_at", "sent_at"]

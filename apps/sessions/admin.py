from django.contrib import admin
from .models import WhatsAppSession, IncomingMessage


@admin.register(WhatsAppSession)
class WhatsAppSessionAdmin(admin.ModelAdmin):
    list_display = ["profile", "status", "phone_number", "last_checked_at", "logged_in_at"]
    list_filter = ["status"]
    search_fields = ["profile__name", "phone_number"]
    readonly_fields = ["created_at", "updated_at", "last_checked_at", "logged_in_at"]


@admin.register(IncomingMessage)
class IncomingMessageAdmin(admin.ModelAdmin):
    list_display = [
        "sender_name", "sender_phone", "message_preview",
        "unread_count", "is_processed", "received_at",
        "session",
    ]
    list_filter = ["is_processed", "session__profile"]
    search_fields = ["sender_name", "sender_phone", "message_preview"]
    readonly_fields = ["received_at", "created_at"]
    list_editable = ["is_processed"]
    ordering = ["-received_at"]

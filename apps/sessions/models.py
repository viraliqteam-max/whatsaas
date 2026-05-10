from django.db import models
from apps.profiles.models import GoLoginProfile


class WhatsAppSession(models.Model):
    """Tracks the WhatsApp Web login state for a GoLogin profile."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QR_REQUIRED = "qr_required", "QR Required"
        LOGGED_IN = "logged_in", "Logged In"
        LOGGED_OUT = "logged_out", "Logged Out"
        ERROR = "error", "Error"

    profile = models.OneToOneField(
        GoLoginProfile, on_delete=models.CASCADE, related_name="whatsapp_session"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    phone_number = models.CharField(max_length=20, blank=True,
                                    help_text="WhatsApp number associated with this session")
    qr_code_base64 = models.TextField(blank=True,
                                      help_text="Base64-encoded QR code PNG (temporary, for login)")
    last_checked_at = models.DateTimeField(null=True, blank=True)
    logged_in_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "WhatsApp Session"

    def __str__(self):
        return f"Session({self.profile.name}) — {self.status}"


class Conversation(models.Model):
    """
    Stable conversation thread keyed by whatsapp_jid (e.g. '919812345678@c.us').

    This is the backbone of conversation-driven routing.  Every auto-reply,
    incoming message record, and outbound task references this model so that
    routing NEVER relies on active-chat DOM state or display names.

    phone is derived from jid (digits before '@') and stored for convenience.
    display_name is updated on each new incoming event but is NOT used for routing.
    """

    profile = models.ForeignKey(
        GoLoginProfile, on_delete=models.CASCADE, related_name="conversations"
    )
    whatsapp_jid = models.CharField(
        max_length=100, db_index=True,
        help_text="WhatsApp JID, e.g. 919812345678@c.us"
    )
    phone = models.CharField(max_length=30, blank=True,
                             help_text="Digits extracted from JID — used for deep-link navigation")
    display_name = models.CharField(max_length=100, blank=True,
                                    help_text="Last known display name (informational only)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("profile", "whatsapp_jid")]
        verbose_name = "Conversation"

    def __str__(self):
        return self.display_name or self.phone or self.whatsapp_jid


class IncomingMessage(models.Model):
    """
    A WhatsApp message received from a contact.
    Populated by the WebSocket incoming_messages event from the Chrome extension.
    """

    session = models.ForeignKey(
        WhatsAppSession, on_delete=models.CASCADE, related_name="incoming_messages"
    )
    conversation = models.ForeignKey(
        Conversation, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="incoming_messages",
        help_text="Resolved stable conversation — set whenever JID is available"
    )
    whatsapp_jid = models.CharField(
        max_length=100, blank=True, default="", db_index=True,
        help_text="WhatsApp JID extracted by the extension sidebar scanner"
    )
    sender_name = models.CharField(
        max_length=255, blank=True,
        help_text="Display name shown in WhatsApp sidebar"
    )
    sender_phone = models.CharField(
        max_length=20, blank=True,
        help_text="Phone number if extractable, else empty"
    )
    message_preview = models.TextField(
        help_text="Last message text visible in the chat sidebar"
    )
    unread_count = models.PositiveIntegerField(default=1)
    is_processed = models.BooleanField(
        default=False,
        help_text="Mark True once you have read/handled this message"
    )
    received_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        verbose_name = "Incoming Message"
        verbose_name_plural = "Incoming Messages"

    def __str__(self):
        return f"From {self.sender_name or self.sender_phone} → {self.session.profile.name}"

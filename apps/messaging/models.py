from django.db import models
from django.contrib.auth.models import User
from apps.profiles.models import GoLoginProfile
from apps.contacts.models import Contact, ContactGroup


class MessageTemplate(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="message_templates")
    name = models.CharField(max_length=255)
    body = models.TextField(help_text="Use {name}, {phone_number} as placeholders")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def render(self, context: dict) -> str:
        """Replace {placeholder} with values from context dict."""
        text = self.body
        for key, value in context.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text


class Campaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        PAUSED = "paused", "Paused"

    class Intent(models.TextChoices):
        OUTREACH = "outreach", "Outreach"
        SALES = "sales", "Sales"
        SUPPORT = "support", "Support"
        FOLLOWUP = "followup", "Follow-up"
        REENGAGEMENT = "reengagement", "Re-engagement"
        ONBOARDING = "onboarding", "Onboarding"
        REMINDER = "reminder", "Reminder"
        WARMUP = "warmup", "Warm-up"

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="campaigns")
    name = models.CharField(max_length=255)
    profile = models.ForeignKey(
        GoLoginProfile, on_delete=models.SET_NULL, null=True, related_name="campaigns"
    )
    template = models.ForeignKey(
        MessageTemplate, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="campaigns"
    )
    custom_message = models.TextField(
        blank=True, help_text="Used if no template is selected. Supports {name}, {phone_number}."
    )
    target_contacts = models.ManyToManyField(Contact, blank=True, related_name="campaigns")
    target_groups = models.ManyToManyField(ContactGroup, blank=True, related_name="campaigns")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    min_delay_seconds = models.PositiveIntegerField(
        default=180, help_text="Minimum seconds to wait between messages (default 3 min)"
    )
    max_delay_seconds = models.PositiveIntegerField(
        default=240, help_text="Maximum seconds to wait between messages (default 4 min)"
    )
    respect_time_windows = models.BooleanField(
        default=True,
        help_text="Only send messages during allowed_time_windows hours"
    )
    campaign_timezone = models.CharField(
        max_length=50,
        default="Asia/Kolkata",
        help_text="Timezone for time windows. E.g. Asia/Kolkata, UTC, America/New_York"
    )
    allowed_time_windows = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'List of allowed sending windows. Leave empty to use default '
            '(8-11am, 1-4pm, 7-10pm). '
            'Format: [{"start":"08:00","end":"11:00"},{"start":"13:00","end":"16:00"}]'
        )
    )
    campaign_intent = models.CharField(
        max_length=20,
        choices=Intent.choices,
        default=Intent.OUTREACH,
        help_text="Intent guides AI message generation tone and content.",
    )
    ai_mode = models.BooleanField(
        default=True,
        help_text=(
            "When ON, the system auto-selects the best profile and generates "
            "a unique, personalized AI message for every contact. "
            "Turn OFF to use a fixed template or custom_message instead."
        ),
    )
    auto_send = models.BooleanField(
        default=False,
        help_text=(
            "When ON, every new contact added by this owner is automatically "
            "messaged via this campaign in real time (no human action needed)."
        ),
    )
    scheduled_at = models.DateTimeField(
        null=True, blank=True, help_text="Leave blank to run immediately on start"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.status})"

    def get_message_for(self, contact: Contact) -> str:
        context = {"name": contact.name, "phone_number": contact.phone_number}
        if self.template:
            return self.template.render(context)
        text = self.custom_message
        for key, value in context.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text

    def get_all_contacts(self):
        """Combine direct contacts and group contacts, deduped."""
        direct = self.target_contacts.all()
        from_groups = Contact.objects.filter(groups__in=self.target_groups.all())
        return (direct | from_groups).distinct()


class MessageLog(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SCHEDULED = "scheduled", "Scheduled"
        DISPATCHED = "dispatched", "Dispatched"
        EXTENSION_RECEIVED = "extension_received", "Extension received"
        OPENING_CHAT = "opening_chat", "Opening chat"
        SENDING = "sending", "Sending"
        ACK_RECEIVED = "ack_received", "ACK received"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        RETRYING = "retrying", "Retrying"
        SKIPPED = "skipped", "Skipped"

    campaign = models.ForeignKey(
        Campaign, on_delete=models.CASCADE, related_name="message_logs", null=True, blank=True
    )
    profile = models.ForeignKey(GoLoginProfile, on_delete=models.SET_NULL, null=True)
    contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True)
    phone_number = models.CharField(max_length=255)
    whatsapp_jid = models.CharField(
        max_length=100, blank=True, default="", db_index=True,
        help_text="WhatsApp JID used for routing, e.g. 919812345678@c.us"
    )
    message_body = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    ack_timeout_attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Msg to {self.phone_number} [{self.status}]"

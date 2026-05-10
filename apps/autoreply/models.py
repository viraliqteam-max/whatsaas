from django.db import models
from django.utils import timezone


class ConversationState(models.Model):
    class Stage(models.TextChoices):
        GREETING = "greeting", "Greeting"
        PROBLEM_DISCOVERY = "problem_discovery", "Problem Discovery"
        CURRENT_STRATEGY = "current_strategy", "Current Strategy"
        SERVICE_MATCHING = "service_matching", "Service Matching"
        LEAD_CAPTURE = "lead_capture", "Lead Capture"
        CTA = "cta", "CTA"
        HUMAN_HANDOFF = "human_handoff", "Human Handoff"

    conversation = models.OneToOneField(
        "whatsapp_sessions.Conversation",
        on_delete=models.CASCADE,
        related_name="ai_state",
    )
    stage = models.CharField(max_length=40, choices=Stage.choices, default=Stage.GREETING)
    detected_language = models.CharField(max_length=20, default="english")
    last_intent = models.CharField(max_length=60, blank=True)
    last_question_key = models.CharField(max_length=80, blank=True)
    last_ai_reply_at = models.DateTimeField(null=True, blank=True)
    human_active = models.BooleanField(default=False)
    ai_paused_until = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Conversation State"

    def __str__(self):
        return f"{self.conversation_id} - {self.stage}"


class LeadProfile(models.Model):
    conversation = models.OneToOneField(
        "whatsapp_sessions.Conversation",
        on_delete=models.CASCADE,
        related_name="lead_profile",
    )
    company_name = models.CharField(max_length=255, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=120, blank=True)
    business_type = models.CharField(max_length=255, blank=True)
    main_problem = models.CharField(max_length=120, blank=True)
    current_marketing_method = models.CharField(max_length=255, blank=True)
    website = models.URLField(blank=True)
    matched_service = models.CharField(max_length=120, blank=True)
    qualification_score = models.PositiveSmallIntegerField(default=0)
    is_qualified = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Lead Profile"

    def __str__(self):
        return self.company_name or self.contact_name or f"Lead {self.conversation_id}"


class ConversationMessage(models.Model):
    class Sender(models.TextChoices):
        USER = "user", "User"
        AI = "ai", "AI"
        HUMAN = "human", "Human"
        SYSTEM = "system", "System"

    conversation = models.ForeignKey(
        "whatsapp_sessions.Conversation",
        on_delete=models.CASCADE,
        related_name="ai_messages",
    )
    sender = models.CharField(max_length=20, choices=Sender.choices)
    text = models.TextField()
    intent = models.CharField(max_length=60, blank=True)
    language = models.CharField(max_length=20, blank=True)
    external_message_id = models.CharField(max_length=120, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
            models.Index(fields=["external_message_id"]),
        ]

    def __str__(self):
        return f"{self.sender}: {self.text[:40]}"


class AIReplyLog(models.Model):
    conversation = models.ForeignKey(
        "whatsapp_sessions.Conversation",
        on_delete=models.CASCADE,
        related_name="ai_reply_logs",
    )
    user_message = models.ForeignKey(
        ConversationMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_reply_logs",
    )
    prompt_hash = models.CharField(max_length=64)
    reply_text = models.TextField(blank=True)
    skipped_reason = models.CharField(max_length=120, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class HandoffEvent(models.Model):
    conversation = models.ForeignKey(
        "whatsapp_sessions.Conversation",
        on_delete=models.CASCADE,
        related_name="handoff_events",
    )
    reason = models.CharField(max_length=120)
    requested_by = models.CharField(max_length=30, default="ai")
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

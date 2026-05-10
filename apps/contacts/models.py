from django.db import models
from django.contrib.auth.models import User
from shared.utils.jid import jid_from_phone


class Contact(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=255)
    phone_number = models.CharField(
        max_length=20,
        help_text="International format without + (e.g. 12025550123)"
    )
    whatsapp_jid = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="Stable WhatsApp JID, e.g. 12025550123@c.us",
    )
    email = models.EmailField(blank=True)
    tags = models.JSONField(default=list, blank=True, help_text='["vip", "lead"]')
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = ["owner", "phone_number"]

    def save(self, *args, **kwargs):
        if not self.whatsapp_jid:
            self.whatsapp_jid = jid_from_phone(self.phone_number)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.phone_number})"


class ContactGroup(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="contact_groups")
    name = models.CharField(max_length=255)
    contacts = models.ManyToManyField(Contact, related_name="groups", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

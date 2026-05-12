from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("whatsapp_sessions", "0002_conversation_incomingmessage_jid"),
    ]

    operations = [
        # ── WhatsAppSession: logged-in account identity ───────────────────────
        migrations.AddField(
            model_name="whatsappsession",
            name="whatsapp_phone_number",
            field=models.CharField(
                blank=True, max_length=20, default="",
                help_text="Phone number of the logged-in WhatsApp account (digits only)",
            ),
        ),
        migrations.AddField(
            model_name="whatsappsession",
            name="logged_in_jid",
            field=models.CharField(
                blank=True, max_length=100, default="",
                help_text="JID of the logged-in account, e.g. 919812345678@c.us",
            ),
        ),
        migrations.AddField(
            model_name="whatsappsession",
            name="account_pushname",
            field=models.CharField(
                blank=True, max_length=100, default="",
                help_text="WhatsApp account display name (pushname)",
            ),
        ),
        # ── Conversation: stable chat identity fields ──────────────────────────
        migrations.AddField(
            model_name="conversation",
            name="pushname",
            field=models.CharField(
                blank=True, max_length=100, default="",
                help_text="Sender's WhatsApp pushname — separate from saved contact name",
            ),
        ),
        migrations.AddField(
            model_name="conversation",
            name="chat_type",
            field=models.CharField(
                blank=True, max_length=20, default="private",
                help_text="Chat type: private / group / business",
            ),
        ),
        migrations.AddField(
            model_name="conversation",
            name="last_message_at",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="Timestamp of the most recent incoming message in this conversation",
            ),
        ),
        migrations.AddField(
            model_name="conversation",
            name="message_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Running count of incoming messages received for this conversation",
            ),
        ),
        # ── IncomingMessage: extraction provenance fields ──────────────────────
        migrations.AddField(
            model_name="incomingmessage",
            name="message_id",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=200,
                help_text="WhatsApp message data-id attribute (stable internal identifier)",
            ),
        ),
        migrations.AddField(
            model_name="incomingmessage",
            name="extraction_method",
            field=models.CharField(
                blank=True, default="", max_length=30,
                help_text="How the JID was extracted: url / react_fiber / react_store / dom_data_id / active_poll",
            ),
        ),
        migrations.AddField(
            model_name="incomingmessage",
            name="extraction_metadata",
            field=models.JSONField(
                blank=True, default=dict,
                help_text="Full extraction metadata payload from the Chrome extension",
            ),
        ),
    ]

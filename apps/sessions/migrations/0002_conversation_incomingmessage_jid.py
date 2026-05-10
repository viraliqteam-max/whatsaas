import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("profiles", "0002_gologinprofile_business_context"),
        ("whatsapp_sessions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Conversation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("whatsapp_jid", models.CharField(
                    db_index=True,
                    help_text="WhatsApp JID, e.g. 919812345678@c.us",
                    max_length=100,
                )),
                ("phone", models.CharField(
                    blank=True,
                    help_text="Digits extracted from JID — used for deep-link navigation",
                    max_length=30,
                )),
                ("display_name", models.CharField(
                    blank=True,
                    help_text="Last known display name (informational only)",
                    max_length=100,
                )),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("profile", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="conversations",
                    to="profiles.gologinprofile",
                )),
            ],
            options={
                "verbose_name": "Conversation",
                "unique_together": {("profile", "whatsapp_jid")},
            },
        ),
        migrations.AddField(
            model_name="incomingmessage",
            name="whatsapp_jid",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=100,
                help_text="WhatsApp JID extracted by the extension sidebar scanner",
            ),
        ),
        migrations.AddField(
            model_name="incomingmessage",
            name="conversation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="incoming_messages",
                to="whatsapp_sessions.conversation",
                help_text="Resolved stable conversation — set whenever JID is available",
            ),
        ),
    ]

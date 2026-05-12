from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("profiles", "0002_gologinprofile_business_context"),
    ]

    operations = [
        migrations.AddField(
            model_name="gologinprofile",
            name="browser_running",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="extension_connected",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="health_status",
            field=models.CharField(
                choices=[
                    ("unknown", "Unknown"),
                    ("healthy", "Healthy"),
                    ("degraded", "Degraded"),
                    ("unhealthy", "Unhealthy"),
                ],
                default="unknown",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="last_heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="runtime_status",
            field=models.CharField(
                choices=[
                    ("stopped", "Stopped"),
                    ("launching", "Launching"),
                    ("browser_open", "Browser open"),
                    ("whatsapp_loading", "WhatsApp loading"),
                    ("whatsapp_ready", "WhatsApp ready"),
                    ("extension_connected", "Extension connected"),
                    ("websocket_connected", "WebSocket connected"),
                    ("ready", "Ready"),
                    ("unhealthy", "Unhealthy"),
                    ("reconnecting", "Reconnecting"),
                ],
                default="stopped",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="sync_status",
            field=models.CharField(
                choices=[
                    ("local_only", "Local only"),
                    ("synced", "Synced"),
                    ("missing_remote", "Missing in GoLogin"),
                    ("sync_error", "Sync error"),
                ],
                default="local_only",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="whatsapp_connected",
            field=models.BooleanField(default=False),
        ),
    ]

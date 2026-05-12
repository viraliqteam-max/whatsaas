from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("profiles", "0003_profile_sync_health_runtime"),
    ]

    operations = [
        # ── New orchestration fields ──────────────────────────────────────────
        migrations.AddField(
            model_name="gologinprofile",
            name="locked",
            field=models.BooleanField(default=False, db_index=True),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="runtime_session_id",
            field=models.UUIDField(null=True, blank=True, db_index=True),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="runtime_owner",
            field=models.CharField(max_length=255, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="browser_pid",
            field=models.IntegerField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="disconnect_reason",
            field=models.CharField(max_length=255, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="playwright_connected",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="websocket_connected",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="gologinprofile",
            name="launched_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        # ── Updated RuntimeStatus choices ─────────────────────────────────────
        migrations.AlterField(
            model_name="gologinprofile",
            name="runtime_status",
            field=models.CharField(
                choices=[
                    ("stopped", "Stopped"),
                    ("launching", "Launching"),
                    ("browser_started", "Browser Started"),
                    ("playwright_connected", "Playwright Connected"),
                    ("extension_connected", "Extension Connected"),
                    ("active", "Active"),
                    ("reconnecting", "Reconnecting"),
                    ("disconnected", "Disconnected"),
                    ("crashed", "Crashed"),
                    ("unhealthy", "Unhealthy"),
                ],
                default="stopped",
                max_length=30,
            ),
        ),
        # ── Compound indexes for common query patterns ────────────────────────
        migrations.AddIndex(
            model_name="gologinprofile",
            index=models.Index(
                fields=["locked", "runtime_status"],
                name="profile_lock_rt_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="gologinprofile",
            index=models.Index(
                fields=["extension_connected", "last_heartbeat_at"],
                name="profile_hb_idx",
            ),
        ),
    ]

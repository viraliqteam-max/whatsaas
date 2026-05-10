from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("messaging", "0005_messagelog_whatsapp_jid"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagelog",
            name="ack_timeout_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="messagelog",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("dispatched", "Dispatched"),
                    ("extension_received", "Extension received"),
                    ("sending", "Sending"),
                    ("ack_received", "ACK received"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("retrying", "Retrying"),
                    ("skipped", "Skipped"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]

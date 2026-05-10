from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("messaging", "0006_messagelog_ack_timeout_attempts_statuses"),
    ]

    operations = [
        migrations.AlterField(
            model_name="messagelog",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("scheduled", "Scheduled"),
                    ("dispatched", "Dispatched"),
                    ("extension_received", "Extension received"),
                    ("opening_chat", "Opening chat"),
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

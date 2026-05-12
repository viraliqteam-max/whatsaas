from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("messaging", "0007_messagelog_more_runtime_statuses"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="campaign_intent",
            field=models.CharField(
                choices=[
                    ("outreach", "Outreach"),
                    ("sales", "Sales"),
                    ("support", "Support"),
                    ("followup", "Follow-up"),
                    ("reengagement", "Re-engagement"),
                    ("onboarding", "Onboarding"),
                    ("reminder", "Reminder"),
                    ("warmup", "Warm-up"),
                ],
                default="outreach",
                help_text="Intent guides AI message generation tone and content.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="ai_mode",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When ON, the system auto-selects the best profile and generates "
                    "a unique, personalized AI message for every contact. "
                    "Turn OFF to use a fixed template or custom_message instead."
                ),
            ),
        ),
    ]

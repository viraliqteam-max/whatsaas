from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("messaging", "0003_campaign_auto_send"),
    ]

    operations = [
        migrations.AlterField(
            model_name="messagelog",
            name="phone_number",
            field=models.CharField(max_length=255),
        ),
    ]

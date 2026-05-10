from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("messaging", "0004_alter_messagelog_phone_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagelog",
            name="whatsapp_jid",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                max_length=100,
                help_text="WhatsApp JID used for routing, e.g. 919812345678@c.us",
            ),
        ),
    ]

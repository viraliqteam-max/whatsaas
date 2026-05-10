from rest_framework import serializers
from .models import Contact, ContactGroup


class ContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contact
        fields = [
            "id", "owner", "name", "phone_number", "whatsapp_jid", "email",
            "tags", "notes", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "owner", "created_at", "updated_at"]


class ContactGroupSerializer(serializers.ModelSerializer):
    contacts = ContactSerializer(many=True, read_only=True)
    contact_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Contact.objects.all(),
        write_only=True, source="contacts", required=False
    )
    contact_count = serializers.SerializerMethodField()

    class Meta:
        model = ContactGroup
        fields = ["id", "owner", "name", "contacts", "contact_ids", "contact_count", "created_at"]
        read_only_fields = ["id", "owner", "created_at"]

    def get_contact_count(self, obj):
        return obj.contacts.count()


class BulkContactSerializer(serializers.Serializer):
    """For importing multiple contacts at once."""
    contacts = serializers.ListField(
        child=serializers.DictField(),
        help_text='[{"name": "John", "phone_number": "12025550123"}, ...]'
    )

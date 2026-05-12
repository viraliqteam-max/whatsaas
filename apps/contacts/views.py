import re

from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django_filters.rest_framework import DjangoFilterBackend

from .models import Contact, ContactGroup
from .serializers import ContactSerializer, ContactGroupSerializer, BulkContactSerializer


class ContactViewSet(viewsets.ModelViewSet):
    """
    Contact management.

    POST   /api/contacts/                  — create contact
    GET    /api/contacts/                  — list contacts (?search=name&tags=vip)
    GET    /api/contacts/{id}/             — retrieve contact
    PUT    /api/contacts/{id}/             — update contact
    DELETE /api/contacts/{id}/             — delete contact
    POST   /api/contacts/bulk_import/      — import multiple contacts
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ContactSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["is_active"]
    search_fields = ["name", "phone_number", "email"]

    def get_queryset(self):
        qs = Contact.objects.filter(owner=self.request.user)
        tags = self.request.query_params.get("tags")
        if tags:
            qs = qs.filter(tags__contains=[tags])
        return qs

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=False, methods=["post"])
    def bulk_import(self, request):
        """Import multiple contacts in one request.

        Accepts either:
          - JSON array of objects: [{"name": "...", "phone_number": "..."}, ...]
          - Objects with phone_number only (name defaults to the number)

        Phone normalization applied to all entries:
          - Strips spaces, dashes, parentheses, leading +
          - 10-digit Indian mobiles (starting 6-9) → prepend 91
          - 11-digit numbers starting with 0 → replace leading 0 with 91
        """
        serializer = BulkContactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        created, skipped = [], []
        for entry in serializer.validated_data["contacts"]:
            raw_phone = str(entry.get("phone_number") or "").strip()
            raw_name = str(entry.get("name") or "").strip()

            digits = re.sub(r"\D", "", raw_phone)
            if len(digits) == 10 and digits[0] in "6789":
                digits = "91" + digits
            elif len(digits) == 11 and digits.startswith("0"):
                digits = "91" + digits[1:]

            if len(digits) < 7:
                skipped.append({"entry": entry, "reason": "invalid phone number"})
                continue

            phone = digits
            name = raw_name or phone

            obj, was_created = Contact.objects.get_or_create(
                owner=request.user,
                phone_number=phone,
                defaults={"name": name, "email": entry.get("email", ""), "notes": entry.get("notes", "")},
            )
            (created if was_created else skipped).append(ContactSerializer(obj).data)

        return Response({"created": created, "skipped": skipped}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def webhook_contact(request):
    """
    POST /api/contacts/webhook/?secret=YOUR_KEY
    Public endpoint — no login required.
    Adds a contact for the configured webhook owner and triggers auto_send campaigns.

    Body: { "name": "...", "phone_number": "...", "email": "...", "tags": [...], "notes": "..." }
    Header or query: X-Webhook-Secret or ?secret=
    """
    from django.conf import settings
    from django.contrib.auth.models import User

    expected = getattr(settings, "WEBHOOK_SECRET_KEY", "")
    provided = request.headers.get("X-Webhook-Secret") or request.GET.get("secret", "")

    if not expected or provided != expected:
        return Response({"error": "Invalid or missing webhook secret"}, status=status.HTTP_403_FORBIDDEN)

    name  = (request.data.get("name") or "").strip()
    phone = (request.data.get("phone_number") or "").strip()
    if not name or not phone:
        return Response({"error": "name and phone_number are required"}, status=status.HTTP_400_BAD_REQUEST)

    # Resolve owner: use WEBHOOK_USER_ID setting or the first superuser
    user_id = getattr(settings, "WEBHOOK_USER_ID", None)
    if user_id:
        user = User.objects.filter(id=user_id).first()
    else:
        user = User.objects.filter(is_superuser=True).order_by("id").first()

    if not user:
        return Response({"error": "No webhook owner configured"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    obj, created = Contact.objects.get_or_create(
        owner=user,
        phone_number=phone,
        defaults={
            "name": name,
            "email": request.data.get("email", ""),
            "notes": request.data.get("notes", ""),
            "tags":  request.data.get("tags", []),
        },
    )

    return Response(
        {"created": created, "id": obj.id, "name": obj.name, "phone_number": obj.phone_number},
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


class ContactGroupViewSet(viewsets.ModelViewSet):
    """
    Contact group management.

    POST   /api/contacts/groups/           — create group
    GET    /api/contacts/groups/           — list groups
    GET    /api/contacts/groups/{id}/      — retrieve group with contacts
    PUT    /api/contacts/groups/{id}/      — update group
    DELETE /api/contacts/groups/{id}/      — delete group
    POST   /api/contacts/groups/{id}/add_contacts/    — add contacts to group
    POST   /api/contacts/groups/{id}/remove_contacts/ — remove contacts from group
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ContactGroupSerializer

    def get_queryset(self):
        return ContactGroup.objects.filter(owner=self.request.user).prefetch_related("contacts")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"])
    def add_contacts(self, request, pk=None):
        group = self.get_object()
        ids = request.data.get("contact_ids", [])
        contacts = Contact.objects.filter(id__in=ids, owner=request.user)
        group.contacts.add(*contacts)
        return Response({"detail": f"Added {contacts.count()} contacts"})

    @action(detail=True, methods=["post"])
    def remove_contacts(self, request, pk=None):
        group = self.get_object()
        ids = request.data.get("contact_ids", [])
        contacts = Contact.objects.filter(id__in=ids, owner=request.user)
        group.contacts.remove(*contacts)
        return Response({"detail": f"Removed {contacts.count()} contacts"})

import csv
import io

from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import path

from .models import Contact, ContactGroup


# ── Actions ──────────────────────────────────────────────────────────────────

def export_csv(modeladmin, request, queryset):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="contacts.csv"'
    writer = csv.writer(response)
    writer.writerow(["name", "phone_number", "email", "tags", "notes"])
    for c in queryset:
        writer.writerow([c.name, c.phone_number, c.email, ",".join(c.tags or []), c.notes])
    return response

export_csv.short_description = "Export selected contacts to CSV"


# ── Contact admin ─────────────────────────────────────────────────────────────

@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display        = ["name", "phone_number", "whatsapp_jid", "email", "is_active", "created_at"]
    list_filter         = ["is_active", "owner"]
    search_fields       = ["name", "phone_number", "whatsapp_jid", "email"]
    actions             = [export_csv]
    change_list_template = "admin/contacts/contact/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv_view),
                name="contacts_contact_import_csv",
            ),
        ]
        return custom + urls

    def import_csv_view(self, request):
        if request.method == "POST":
            return self._handle_import(request)
        return render(request, "admin/contacts/contact/import_csv.html")

    def _handle_import(self, request):
        csv_file = request.FILES.get("csv_file")
        if not csv_file:
            messages.error(request, "No file selected.")
            return redirect(".")

        try:
            decoded   = csv_file.read().decode("utf-8-sig")
            reader    = csv.DictReader(io.StringIO(decoded))
            created   = 0
            skipped   = 0
            owner     = request.user

            for row in reader:
                phone = row.get("phone_number", "").strip().lstrip("+")
                name  = row.get("name", "").strip()
                if not phone:
                    skipped += 1
                    continue

                tags_raw = row.get("tags", "")
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

                _, was_created = Contact.objects.get_or_create(
                    owner=owner,
                    phone_number=phone,
                    defaults={
                        "name":  name or phone,
                        "email": row.get("email", "").strip(),
                        "notes": row.get("notes", "").strip(),
                        "tags":  tags,
                    },
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1

            messages.success(
                request,
                f"Import complete — {created} contacts added, {skipped} duplicates skipped. "
                f"Auto-send campaigns will message new contacts immediately.",
            )
        except Exception as exc:
            messages.error(request, f"Import failed: {exc}")

        return redirect("../")


# ── ContactGroup admin ────────────────────────────────────────────────────────

@admin.register(ContactGroup)
class ContactGroupAdmin(admin.ModelAdmin):
    list_display     = ["name", "owner", "created_at"]
    filter_horizontal = ["contacts"]

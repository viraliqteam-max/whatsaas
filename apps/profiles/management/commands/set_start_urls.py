from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.conf import settings

from apps.profiles.models import GoLoginProfile
from utils import gologin_manager as gl


class Command(BaseCommand):
    help = "Fetch real GoLogin profiles, set startUrl on each, and sync the local DB."

    def handle(self, *args, **options):
        backend_url = getattr(settings, "BACKEND_URL", "http://localhost:8000").rstrip("/")

        # Fetch live profiles from GoLogin API
        try:
            data = gl.list_gologin_profiles(limit=200)
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"GoLogin API error: {exc}"))
            return

        remote_profiles = data.get("profiles", [])
        if not remote_profiles:
            self.stdout.write(self.style.WARNING("No profiles found in your GoLogin account."))
            return

        self.stdout.write(f"Found {len(remote_profiles)} GoLogin profile(s). Syncing...\n")

        owner = User.objects.filter(is_superuser=True).first()
        ok = 0
        failed = 0

        for p in remote_profiles:
            pid  = p["id"]
            name = p["name"]
            start_url = f"{backend_url}/ext-init/{pid}/"

            # Set startUrl via GoLogin API
            try:
                gl.update_gologin_profile(pid, {"startUrl": start_url})
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  FAIL  {name} ({pid})  ->  {exc}"))
                failed += 1
                continue

            # Upsert DB record so the profile is visible in the backend
            GoLoginProfile.objects.update_or_create(
                gologin_profile_id=pid,
                defaults={"name": name, "owner": owner},
            )

            self.stdout.write(self.style.SUCCESS(f"  OK  {name} ({pid})  ->  {start_url}"))
            ok += 1

        self.stdout.write(f"\nDone -- {ok} updated, {failed} failed.")

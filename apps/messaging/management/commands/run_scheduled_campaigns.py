from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Dispatch scheduled campaigns to the Chrome extension (no Redis needed)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would run without actually dispatching",
        )

    def handle(self, *args, **options):
        from apps.campaigns.services.scheduler import dispatch_due_campaigns

        results = dispatch_due_campaigns(dry_run=options["dry_run"])
        if not results:
            self.stdout.write("No campaigns due to run.")
            return

        for result in results:
            campaign = result["campaign"]
            status = result["status"]

            if status == "skipped":
                reason = result["reason"].replace("_", " ")
                self.stdout.write(self.style.WARNING(f"Campaign '{campaign.name}' skipped - {reason}"))
            elif status == "dry_run":
                self.stdout.write(
                    f"[DRY RUN] Would dispatch campaign '{campaign.name}' "
                    f"to {result['contacts']} contacts via profile {result['profile_id']}"
                )
            elif status == "dispatched":
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Dispatched {result['dispatched']}/{result['contacts']} "
                        f"messages for '{campaign.name}'"
                    )
                )

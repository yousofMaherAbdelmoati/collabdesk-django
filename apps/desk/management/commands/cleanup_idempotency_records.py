from django.core.management.base import BaseCommand

from apps.desk.services import cleanup_expired_idempotency_records


class Command(BaseCommand):
    help = "Delete expired persisted ticket idempotency records."

    def handle(self, *args, **options):
        deleted = cleanup_expired_idempotency_records()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired idempotency records."))

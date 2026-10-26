import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.reports.benchmarking import SUPPORTED_SIZES, seed_benchmark_tickets


class Command(BaseCommand):
    help = "Append deterministic, fictional DJ-012 benchmark tickets; never resets tables."

    def add_arguments(self, parser):
        parser.add_argument("--size", type=int, choices=SUPPORTED_SIZES, required=True)
        parser.add_argument("--seed", type=int, default=12012)
        parser.add_argument("--namespace", required=True, help="Unique local fixture namespace (letters, numbers, hyphens).")
        parser.add_argument("--confirm-fictional-data", action="store_true", help="Required acknowledgement that this creates fictional local data.")

    def handle(self, *args, **options):
        if not settings.LOCAL_DEMO_SEED_ENABLED:
            raise CommandError("Benchmark seeding is disabled outside DJANGO_ENV=development/dev/local/test.")
        if not options["confirm_fictional_data"]:
            raise CommandError("Pass --confirm-fictional-data; this command appends fictional data and never resets tables.")
        try:
            manifest = seed_benchmark_tickets(size=options["size"], seed=options["seed"], namespace=options["namespace"])
        except (ValueError, RuntimeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(manifest, sort_keys=True))

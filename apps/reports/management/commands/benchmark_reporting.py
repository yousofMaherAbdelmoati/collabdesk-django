import json
from pathlib import Path

from django.conf import settings
from django.core.cache import caches
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings
from django.utils import timezone

from apps.reports.benchmarking import (
    SUPPORTED_SIZES,
    ensure_precomputed_result,
    run_benchmark,
    runtime_configuration,
    seed_benchmark_tickets,
)


class Command(BaseCommand):
    help = "Run opt-in DJ-012 reporting benchmark over tagged fictional fixtures."

    def add_arguments(self, parser):
        parser.add_argument("--size", type=int, choices=SUPPORTED_SIZES, required=True)
        parser.add_argument("--seed", type=int, default=12012)
        parser.add_argument("--namespace", required=True)
        parser.add_argument("--warmups", type=int, default=2)
        parser.add_argument("--iterations", type=int, default=5)
        parser.add_argument("--cache-backend", choices=("configured", "locmem"), default="configured")
        parser.add_argument("--output", required=True, help="Path for a raw JSON result; existing file is replaced.")
        parser.add_argument("--confirm-fictional-data", action="store_true")

    def handle(self, *args, **options):
        if not settings.LOCAL_DEMO_SEED_ENABLED:
            raise CommandError("Benchmarking is disabled outside DJANGO_ENV=development/dev/local/test.")
        if not options["confirm_fictional_data"]:
            raise CommandError("Pass --confirm-fictional-data; this command appends only tagged fictional data.")
        output = Path(options["output"])
        if output.is_absolute() or ".." in output.parts:
            raise CommandError("--output must be a relative path below this repository")
        manifest = seed_benchmark_tickets(size=options["size"], seed=options["seed"], namespace=options["namespace"])
        cache_settings = settings.CACHES
        if options["cache_backend"] == "locmem":
            cache_settings = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": f"dj012-{manifest['namespace']}"}}
        try:
            with override_settings(CACHES=cache_settings):
                caches.close_all()
                job = ensure_precomputed_result(manifest=manifest)
                result = {
                    "benchmark": "DJ-012 reporting and caching",
                    "started_at": timezone.now().isoformat(),
                    "fixture": manifest,
                    "configuration": runtime_configuration(),
                    "cache_mode": options["cache_backend"],
                    "measurements": run_benchmark(job=job, warmups=options["warmups"], iterations=options["iterations"]),
                }
        except (ValueError, RuntimeError) as exc:
            raise CommandError(str(exc)) from exc
        finally:
            caches.close_all()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        self.stdout.write(f"Wrote raw benchmark result to {output}")

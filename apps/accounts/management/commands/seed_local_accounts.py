from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User


class Command(BaseCommand):
    help = "Create fictional customer, agent, and admin accounts for local development only."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True, help="Password applied to the fictional local accounts")

    def handle(self, *args, **options):
        if not settings.LOCAL_DEMO_SEED_ENABLED:
            raise CommandError("Local demo seeding is disabled outside DJANGO_ENV=development/dev/local/test.")
        password = options["password"]
        accounts = [
            ("demo-customer", User.Role.CUSTOMER, False),
            ("demo-agent", User.Role.AGENT, False),
            ("demo-admin", User.Role.ADMIN, False),
        ]
        for username, role, superuser in accounts:
            user, created = User.objects.get_or_create(username=username, defaults={"role": role})
            user.role = role
            user.is_superuser = superuser
            user.set_password(password)
            user.save()
            self.stdout.write(f"{'Created' if created else 'Updated'} {username} ({role})")

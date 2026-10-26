from django.apps import AppConfig
class DeskConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.desk"

    def ready(self):
        # Register cache invalidation only after the app registry is ready.
        # Importing here avoids model-import cycles during Django startup.
        from . import signals  # noqa: F401

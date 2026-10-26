"""Small server-rendered shell for the local CollabDesk browser demo."""

from django.conf import settings
from django.shortcuts import render


def demo_ui(request):
    """Render the browser client without exposing a development test API.

    The only environment-specific value is used to omit local test controls
    from production-like pages.  The controls themselves use the ordinary,
    authenticated ticket API and introduce no privileged endpoints.
    """
    return render(request, "index.html", {"local_demo": settings.LOCAL_DEMO_SEED_ENABLED})

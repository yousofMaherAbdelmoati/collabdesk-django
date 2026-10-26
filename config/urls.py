from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from apps.desk.frontend import demo_ui

urlpatterns = [
    path("", demo_ui, name="demo-ui"),
    path("admin/", admin.site.urls), path("api/", include("apps.desk.urls")),
    path("api/", include("apps.accounts.urls")),
    path("api/", include("apps.reports.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema")),
]

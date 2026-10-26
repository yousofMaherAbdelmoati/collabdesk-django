from django.urls import path
from . import views
urlpatterns = [
    path("reports", views.reports),
    path("reports/<int:job_id>", views.report_detail),
    path("reports/<int:job_id>/download", views.report_download),
]

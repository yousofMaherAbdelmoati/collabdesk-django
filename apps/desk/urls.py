from django.urls import path
from . import views
urlpatterns = [
    path("health/", views.health),
    path("health/live/", views.liveness),
    path("health/ready/", views.readiness),
    path("tickets", views.tickets),
    path("tickets/stats", views.ticket_statistics),
    path("tickets/cache-metrics", views.ticket_cache_metrics),
    path("tickets/<int:ticket_id>/claim", views.claim),
    path("tickets/<int:ticket_id>/status", views.transition_status),
    path("tickets/<int:ticket_id>/messages", views.messages),
    path("assistant/stream", views.assistant_stream),
]

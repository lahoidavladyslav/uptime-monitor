from django.urls import path

from . import views

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("monitors/new/", views.MonitorCreateView.as_view(), name="monitor_new"),
    path("monitors/<int:pk>/", views.MonitorDetailView.as_view(), name="monitor_detail"),
    path("monitors/<int:pk>/edit/", views.MonitorUpdateView.as_view(), name="monitor_edit"),
    path("monitors/<int:pk>/delete/", views.MonitorDeleteView.as_view(), name="monitor_delete"),
    path("monitors/<int:pk>/check/", views.CheckNowView.as_view(), name="monitor_check"),
    path("monitors/<int:pk>/maintenance/", views.MaintenanceCreateView.as_view(), name="maintenance_create"),
    path(
        "monitors/<int:pk>/maintenance/<int:window_id>/delete/",
        views.MaintenanceDeleteView.as_view(),
        name="maintenance_delete",
    ),
    path("status/<slug:slug>/", views.public_status, name="public_status"),
]

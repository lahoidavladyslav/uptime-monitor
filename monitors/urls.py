from django.urls import path

from . import public, views

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
    path("pages/", views.StatusPageListView.as_view(), name="statuspage_list"),
    path("pages/new/", views.StatusPageCreateView.as_view(), name="statuspage_new"),
    path("pages/<int:pk>/edit/", views.StatusPageUpdateView.as_view(), name="statuspage_edit"),
    path("pages/<int:pk>/delete/", views.StatusPageDeleteView.as_view(), name="statuspage_delete"),
    path("status/<slug:slug>/", public.monitor_status, name="public_status"),
    path("status/<slug:slug>/feed/", public.monitor_feed, name="monitor_feed"),
    path("page/<slug:slug>/", public.group_status, name="group_status"),
    path("page/<slug:slug>/feed/", public.group_feed, name="group_feed"),
]

from django.urls import path

from . import views

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("monitors/new/", views.MonitorCreateView.as_view(), name="monitor_new"),
    path("monitors/<int:pk>/", views.MonitorDetailView.as_view(), name="monitor_detail"),
    path("monitors/<int:pk>/edit/", views.MonitorUpdateView.as_view(), name="monitor_edit"),
    path("monitors/<int:pk>/delete/", views.MonitorDeleteView.as_view(), name="monitor_delete"),
    path("monitors/<int:pk>/check/", views.CheckNowView.as_view(), name="monitor_check"),
    path("status/<slug:slug>/", views.public_status, name="public_status"),
]

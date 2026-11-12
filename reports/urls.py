from django.urls import path

from . import views

urlpatterns = [
    path("", views.report_list, name="report_list"),
    path("<int:pk>/download/", views.report_download, name="report_download"),
]

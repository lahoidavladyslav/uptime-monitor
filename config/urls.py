from django.contrib import admin
from django.urls import include, path

from api.api import api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("", include("accounts.urls")),
    path("reports/", include("reports.urls")),
    path("", include("monitors.urls")),
]

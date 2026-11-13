from django.conf import settings
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

if "debug_toolbar" in settings.INSTALLED_APPS:  # only when DEBUG_TOOLBAR=1 in development
    import debug_toolbar

    urlpatterns.insert(0, path("__debug__/", include(debug_toolbar.urls)))

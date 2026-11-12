from django.contrib import admin

from .models import CheckResult, Incident, Monitor

admin.site.register(Monitor)
admin.site.register(CheckResult)
admin.site.register(Incident)

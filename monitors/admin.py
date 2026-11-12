from django.contrib import admin

from .models import Incident, Monitor

admin.site.register(Monitor)
# CheckResult is not registered: Django's admin cannot handle a composite primary key (and a browsable list of
# millions of raw rows would be useless anyway; use the monitor page).
admin.site.register(Incident)

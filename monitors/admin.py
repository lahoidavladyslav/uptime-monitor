from django.contrib import admin
from django.db.models import Count, Q

from .models import Incident, Monitor, StatusSubscriber


@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "check_type", "is_active", "confirmed_subscribers", "unsubscribed_count")
    list_filter = ("check_type", "is_active", "is_public")
    search_fields = ("name", "url", "user__username")
    readonly_fields = ("confirmed_subscribers", "unsubscribed_count")

    def get_queryset(self, request):
        # one grouped query: the counts must not cost a query per row
        return (
            super()
            .get_queryset(request)
            .select_related("user")
            .annotate(
                _confirmed=Count(
                    "subscribers",
                    filter=Q(subscribers__confirmed_at__isnull=False, subscribers__unsubscribed_at__isnull=True),
                ),
                _unsubscribed=Count("subscribers", filter=Q(subscribers__unsubscribed_at__isnull=False)),
            )
        )

    @admin.display(description="Підтверджених підписників", ordering="_confirmed")
    def confirmed_subscribers(self, obj):
        return obj._confirmed

    @admin.display(description="Відписок", ordering="_unsubscribed")
    def unsubscribed_count(self, obj):
        return obj._unsubscribed


@admin.register(StatusSubscriber)
class StatusSubscriberAdmin(admin.ModelAdmin):
    list_display = ("email", "monitor", "status_page", "confirmed_at", "unsubscribed_at", "created_at")
    list_filter = ("confirmed_at", "unsubscribed_at")
    search_fields = ("email",)
    readonly_fields = ("unsub_token",)


# CheckResult is not registered: Django's admin cannot handle a composite primary key (and a browsable list of
# millions of raw rows would be useless anyway; use the monitor page).
admin.site.register(Incident)

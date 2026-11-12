from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, OuterRef, Q, Subquery
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView
from django.views.generic.edit import FormView

from .forms import MonitorForm, SignupForm
from .models import CheckResult, Incident, Monitor
from .services import PERIODS, daily_bar, monitor_stats, response_time_series, uptime_percent
from .signals import status_cache_key
from .tasks import check_monitor


class SignupView(FormView):
    template_name = "registration/signup.html"
    form_class = SignupForm
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        user = form.save()
        login(self.request, user)
        return super().form_valid(form)


class OwnedMonitorMixin(LoginRequiredMixin):
    """Every query is scoped to request.user -> foreign ids are a plain 404."""

    def get_queryset(self):
        return Monitor.objects.filter(user=self.request.user)


class HtmxLoginRequiredMixin(LoginRequiredMixin):
    """For HTMX polling: an expired session must not swap the login page into a table."""

    def handle_no_permission(self):
        if self.request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = f"{reverse('login')}?next={self.request.path}"
            return response
        return super().handle_no_permission()


class DashboardView(HtmxLoginRequiredMixin, ListView):
    template_name = "monitors/dashboard.html"
    context_object_name = "monitors"

    def get_template_names(self):
        # The polling request only needs the table, not the whole page.
        if self.request.headers.get("HX-Request"):
            return ["monitors/_dashboard_table.html"]
        return [self.template_name]

    def get_queryset(self):
        since = timezone.now() - timedelta(hours=24)
        latest = CheckResult.objects.filter(monitor=OuterRef("pk")).order_by("-checked_at")
        open_incident = Incident.objects.filter(monitor=OuterRef("pk"), resolved_at__isnull=True)
        # One query for the whole page: no per-monitor lookups.
        return Monitor.objects.filter(user=self.request.user).annotate(
            last_is_up=Subquery(latest.values("is_up")[:1]),
            down_since=Subquery(open_incident.values("started_at")[:1]),
            checks_24h=Count("checks", filter=Q(checks__checked_at__gte=since)),
            up_24h=Count("checks", filter=Q(checks__checked_at__gte=since, checks__is_up=True)),
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        for m in ctx["monitors"]:
            m.uptime_24h = uptime_percent(m.checks_24h, m.up_24h)
            m.down_minutes = int((now - m.down_since).total_seconds() // 60) if m.down_since else None
        return ctx


class MonitorCreateView(LoginRequiredMixin, CreateView):
    model = Monitor
    form_class = MonitorForm
    template_name = "monitors/monitor_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, "Монітор створено.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("monitor_detail", args=[self.object.pk])


class MonitorUpdateView(OwnedMonitorMixin, UpdateView):
    form_class = MonitorForm
    template_name = "monitors/monitor_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        messages.success(self.request, "Зміни збережено.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("monitor_detail", args=[self.object.pk])


class MonitorDeleteView(OwnedMonitorMixin, DeleteView):
    template_name = "monitors/monitor_confirm_delete.html"
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        messages.success(self.request, "Монітор видалено.")
        return super().form_valid(form)


class MonitorDetailView(OwnedMonitorMixin, View):
    def get(self, request, pk):
        monitor = get_object_or_404(self.get_queryset(), pk=pk)
        period = request.GET.get("period", "24h")
        if period not in PERIODS:
            period = "24h"
        only_failed = request.GET.get("only_failed") == "1"

        checks = monitor.checks.all()
        if only_failed:
            checks = checks.filter(is_up=False)
        page = Paginator(checks, 20).get_page(request.GET.get("page"))

        return render(
            request,
            "monitors/monitor_detail.html",
            {
                "monitor": monitor,
                "period": period,
                "stats": monitor_stats(monitor, period),
                "series": response_time_series(monitor, period),
                "only_failed": only_failed,
                "page": page,
                "incidents": monitor.incidents.all()[:50],
            },
        )


def _cooldown_ok(key, seconds):
    """cache.add is atomic: only the first caller within `seconds` gets True.

    If the cache is down we fail open (better to allow a click than to 500).
    """
    try:
        return cache.add(key, 1, seconds)
    except Exception:
        return True


class CheckNowView(OwnedMonitorMixin, View):
    def post(self, request, pk):
        monitor = get_object_or_404(self.get_queryset(), pk=pk)
        if not _cooldown_ok(f"check-now:{monitor.pk}", settings.CHECK_NOW_COOLDOWN_SECONDS):
            messages.warning(request, "Зачекайте кілька секунд перед наступною перевіркою.")
            return redirect("monitor_detail", pk=monitor.pk)
        check_monitor.delay(monitor.pk, force=True)
        messages.success(request, "Перевірку запущено")
        return redirect("monitor_detail", pk=monitor.pk)


def public_status(request, slug):
    """Public, login-free, cached whole-page for 60s.

    Only the DB-hit path decides 404; a 404 is never cached. Invalidation on
    Monitor save/delete (signals.py) makes "go private" effective at once.
    """
    key = status_cache_key(slug)
    html = cache.get(key)
    if html is None:
        monitor = Monitor.objects.filter(public_slug=slug, is_public=True).first()
        if monitor is None:
            raise Http404
        stats = monitor_stats(monitor, "7d")
        last = monitor.checks.order_by("-checked_at").values_list("is_up", flat=True).first()
        html = render_to_string(
            "monitors/status.html",
            {"name": monitor.name, "is_up": last, "uptime": stats["uptime"], "bar": daily_bar(monitor, 7)},
            request,
        )
        cache.set(key, html, settings.PUBLIC_STATUS_CACHE_SECONDS)
    return HttpResponse(html)

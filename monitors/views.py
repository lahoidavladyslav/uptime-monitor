from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from . import maintenance
from .formkit import FormErrorMessageMixin
from .forms import MaintenanceForm, MonitorForm, StatusPageForm
from .models import MaintenanceWindow, Monitor, StatusPage
from .regions import region_status
from .services import (
    PERIODS,
    attach_counts_24h,
    cooldown_ok,
    dashboard_queryset,
    monitor_stats,
    response_time_series,
    ssl_days_left,
    uptime_percent,
    uptime_windows,
)
from .tasks import check_ssl, enqueue_checks


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
        return dashboard_queryset(self.request.user, counts=False)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        attach_counts_24h(ctx["monitors"], now)
        for m in ctx["monitors"]:
            m.uptime_24h = uptime_percent(m.checks_24h, m.up_24h)
            m.down_minutes = int((now - m.down_since).total_seconds() // 60) if m.down_since else None
        return ctx


class MonitorCreateView(FormErrorMessageMixin, LoginRequiredMixin, CreateView):
    model = Monitor
    form_class = MonitorForm
    template_name = "monitors/monitor_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, "Монітор створено.")
        response = super().form_valid(form)
        pk = self.object.pk
        if self.object.url.lower().startswith("https://"):
            transaction.on_commit(lambda: check_ssl.delay(pk))
        return response

    def get_success_url(self):
        return reverse_lazy("monitor_detail", args=[self.object.pk])


class MonitorUpdateView(FormErrorMessageMixin, OwnedMonitorMixin, UpdateView):
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

        windows = uptime_windows(monitor, (30, 90))
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
                "ssl_days_left": ssl_days_left(monitor),
                "region_rows": region_status(monitor),
                "uptime_30d": windows[30],
                "uptime_90d": windows[90],
                "windows": maintenance.visible_for(monitor)[:10],
                "maintenance_form": MaintenanceForm(),
                "in_maintenance": maintenance.is_active(monitor, timezone.now()),
            },
        )


class CheckNowView(OwnedMonitorMixin, View):
    def post(self, request, pk):
        monitor = get_object_or_404(self.get_queryset(), pk=pk)
        if not cooldown_ok(f"check-now:{monitor.pk}", settings.CHECK_NOW_COOLDOWN_SECONDS):
            messages.warning(request, "Зачекайте кілька секунд перед наступною перевіркою.")
            return redirect("monitor_detail", pk=monitor.pk)
        enqueue_checks(monitor.pk, force=True)
        if monitor.url.lower().startswith("https://"):
            check_ssl.delay(monitor.pk)
        messages.success(request, "Перевірку запущено")
        return redirect("monitor_detail", pk=monitor.pk)


class MaintenanceCreateView(OwnedMonitorMixin, View):
    def post(self, request, pk):
        monitor = get_object_or_404(self.get_queryset(), pk=pk)
        form = MaintenanceForm(request.POST)
        if form.is_valid():
            maintenance.save_from_form(form, monitor)
            messages.success(
                request, "Вікно обслуговування додано: перевірки тривають, але інциденти не відкриваються."
            )
        else:
            for error in sum(form.errors.values(), []):
                messages.error(request, error)
        return redirect("monitor_detail", pk=monitor.pk)


class MaintenanceDeleteView(OwnedMonitorMixin, View):
    def post(self, request, pk, window_id):
        monitor = get_object_or_404(self.get_queryset(), pk=pk)
        get_object_or_404(
            MaintenanceWindow.objects.filter(maintenance.applies_to(monitor.pk, monitor.user_id)), pk=window_id
        ).delete()
        messages.success(request, "Вікно видалено.")
        return redirect("monitor_detail", pk=monitor.pk)


class StatusPageListView(LoginRequiredMixin, ListView):
    template_name = "monitors/statuspage_list.html"
    context_object_name = "pages"

    def get_queryset(self):
        return StatusPage.objects.filter(user=self.request.user).prefetch_related("monitors")


class OwnedPageMixin(LoginRequiredMixin):
    def get_queryset(self):
        return StatusPage.objects.filter(user=self.request.user)


class StatusPageCreateView(FormErrorMessageMixin, LoginRequiredMixin, CreateView):
    model = StatusPage
    form_class = StatusPageForm
    template_name = "monitors/statuspage_form.html"
    success_url = reverse_lazy("statuspage_list")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, "Статус-сторінку створено.")
        return super().form_valid(form)


class StatusPageUpdateView(FormErrorMessageMixin, OwnedPageMixin, UpdateView):
    form_class = StatusPageForm
    template_name = "monitors/statuspage_form.html"
    success_url = reverse_lazy("statuspage_list")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        messages.success(self.request, "Зміни збережено.")
        return super().form_valid(form)


class StatusPageDeleteView(OwnedPageMixin, DeleteView):
    template_name = "monitors/statuspage_confirm_delete.html"
    success_url = reverse_lazy("statuspage_list")

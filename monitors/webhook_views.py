"""/monitors/{id}/webhooks/: endpoints, the delivery log, and "Повторити" for dead events."""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from . import webhooks
from .forms import WebhookEndpointForm
from .models import Monitor, OutboxEvent, WebhookEndpoint

LOG_SIZE = 50


class OwnedMonitor(LoginRequiredMixin, View):
    def monitor(self, request, pk):
        return get_object_or_404(Monitor, pk=pk, user=request.user)  # foreign id: 404, never 403


class WebhooksView(OwnedMonitor):
    def get(self, request, pk):
        monitor = self.monitor(request, pk)
        events = (
            OutboxEvent.objects.filter(endpoint__monitor=monitor).select_related("endpoint").order_by("-id")[:LOG_SIZE]
        )
        return render(
            request,
            "monitors/webhooks.html",
            {
                "monitor": monitor,
                "endpoints": monitor.webhooks.all(),
                "events": events,
                "form": WebhookEndpointForm(),
                "can_add": monitor.webhooks.count() < webhooks.MAX_ENDPOINTS,
            },
        )


class WebhookCreateView(OwnedMonitor):
    def get(self, request, pk):
        self.monitor(request, pk)
        return redirect("webhooks", pk=pk)

    def post(self, request, pk):
        monitor = self.monitor(request, pk)
        if monitor.webhooks.count() >= webhooks.MAX_ENDPOINTS:
            messages.error(request, f"Не більше {webhooks.MAX_ENDPOINTS} webhook-ів на монітор.")
            return redirect("webhooks", pk=pk)
        form = WebhookEndpointForm(request.POST)
        if form.is_valid():
            endpoint = form.save(commit=False)
            endpoint.monitor = monitor
            endpoint.save()
            messages.success(request, "Webhook додано. Секрет для перевірки підпису показано нижче.")
        else:
            for error in sum(form.errors.values(), []):
                messages.error(request, error)
        return redirect("webhooks", pk=pk)


class WebhookDeleteView(OwnedMonitor):
    def post(self, request, pk, endpoint_id):
        monitor = self.monitor(request, pk)
        get_object_or_404(WebhookEndpoint, pk=endpoint_id, monitor=monitor).delete()
        messages.success(request, "Webhook видалено.")
        return redirect("webhooks", pk=pk)


class WebhookRetryView(OwnedMonitor):
    def post(self, request, pk, event_id):
        monitor = self.monitor(request, pk)
        event = get_object_or_404(OutboxEvent, pk=event_id, endpoint__monitor=monitor, status=OutboxEvent.Status.DEAD)
        OutboxEvent.objects.filter(pk=event.pk, status=OutboxEvent.Status.DEAD).update(
            status=OutboxEvent.Status.PENDING, attempts=0, next_attempt_at=timezone.now(), processed_at=None
        )
        messages.success(request, "Подію поставлено на повторну доставку.")
        return redirect("webhooks", pk=pk)

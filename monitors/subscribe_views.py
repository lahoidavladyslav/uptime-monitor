"""Login-free subscription pages. Deliberately NOT cached: they carry a CSRF token."""

from django import forms
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from config.observability import client_ip

from . import subscriptions
from .formkit import BootstrapFormMixin
from .models import Monitor, StatusPage, StatusSubscriber


class SubscribeForm(BootstrapFormMixin, forms.Form):
    email = forms.EmailField(label="Ваш email", max_length=254)


def _subscribe(request, target, title, back_url):
    form = SubscribeForm(request.POST or None)
    if request.method == "POST":
        if subscriptions.rate_limited(client_ip(request)):
            return render(request, "monitors/subscribe_limited.html", {"back_url": back_url}, status=429)
        if form.is_valid():
            subscriptions.request_subscription(target, form.cleaned_data["email"])
            # identical answer whether the address is new, pending, or already confirmed
            return render(request, "monitors/subscribe_sent.html", {"title": title, "back_url": back_url})
    return render(request, "monitors/subscribe.html", {"form": form, "title": title, "back_url": back_url})


@require_http_methods(["GET", "POST"])
def group_subscribe(request, slug):
    page = get_object_or_404(StatusPage, slug=slug, is_public=True)
    return _subscribe(request, page, page.title, f"/page/{page.slug}/")


@require_http_methods(["GET", "POST"])
def monitor_subscribe(request, slug):
    monitor = get_object_or_404(Monitor, public_slug=slug, is_public=True)
    return _subscribe(request, monitor, monitor.public_name, f"/status/{slug}/")


def confirm_subscription(request, token):
    sub = subscriptions.confirm(token)
    if sub is None:
        return render(request, "monitors/subscribe_confirm_failed.html", status=400)
    return render(request, "monitors/subscribe_confirmed.html", {"unsubscribe_url": subscriptions.unsubscribe_url(sub)})


@csrf_exempt  # RFC 8058 one-click: mail clients POST here with no cookie; the token IS the credential
@require_http_methods(["GET", "POST"])
def unsubscribe(request, token):
    """GET only shows a button (mail scanners prefetch links and must not unsubscribe people)."""
    if request.method == "POST":
        subscriptions.unsubscribe(token)
        return render(request, "monitors/unsubscribed.html")
    exists = StatusSubscriber.objects.filter(unsub_token=token).exists()
    return render(request, "monitors/unsubscribe_confirm.html", {"exists": exists, "token": token})

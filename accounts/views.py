import io
import time

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic.edit import FormView

from . import services
from .forms import CodeForm, LoginForm, ResendForm, SignupForm
from .models import LoginEvent, TOTPDevice

User = get_user_model()


# ---------- email confirmation ----------
def send_confirmation(request, user):
    token = services.make_confirmation_token(user)
    services.send_confirmation_email(user, request.build_absolute_uri(reverse("confirm_email", args=[token])))


class SignupView(FormView):
    template_name = "registration/signup.html"
    form_class = SignupForm

    def form_valid(self, form):
        user = form.save(commit=False)
        user.is_active = False  # cannot log in until the email is confirmed
        user.save()
        send_confirmation(self.request, user)
        return render(self.request, "registration/signup_done.html", {"email": user.email})


def confirm_email(request, token):
    if services.confirm_token(token) is None:
        return render(request, "registration/confirm_failed.html", status=400)
    messages.success(request, "Email підтверджено. Тепер можна увійти.")
    return redirect("login")


def resend_confirmation(request):
    """Always answers the same way, so it can't be used to discover which emails exist."""
    form = ResendForm(request.POST or None)
    if request.method == "POST" and form.is_valid():

        def link_for(user):
            token = services.make_confirmation_token(user)
            return request.build_absolute_uri(reverse("confirm_email", args=[token]))

        services.resend_confirmation(form.cleaned_data["email"], link_for)
        messages.info(request, "Якщо такий неактивований акаунт існує, ми надіслали лист повторно.")
        return redirect("login")
    return render(request, "registration/resend.html", {"form": form})


# ---------- login with optional TOTP ----------
class TwoFactorLoginView(LoginView):
    form_class = LoginForm

    def form_valid(self, form):
        user = form.get_user()
        if not services.has_two_factor(user):
            return super().form_valid(form)
        services.begin_two_factor(self.request, user, self.get_redirect_url())
        return redirect("two_factor_verify")


def two_factor_verify(request):
    pre = request.session.get("pre_2fa")
    if not pre or time.time() - pre["ts"] > services.PRE_2FA_MAX_AGE:
        request.session.pop("pre_2fa", None)
        messages.warning(request, "Сесія входу завершилась. Увійдіть знову.")
        return redirect("login")
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        next_url = pre["next"]
        result = services.finish_two_factor(request, form.cleaned_data["code"])
        if result == "locked":
            messages.error(request, "Забагато невдалих спроб. Увійдіть знову.")
            return redirect("login")
        if result == "expired":
            messages.warning(request, "Сесія входу завершилась. Увійдіть знову.")
            return redirect("login")
        if result == "ok":
            if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
                next_url = settings.LOGIN_REDIRECT_URL
            return redirect(next_url or settings.LOGIN_REDIRECT_URL)
        form.add_error("code", "Невірний код.")
    return render(request, "registration/two_factor_verify.html", {"form": form})


# ---------- 2FA management ----------
def _qr_svg(uri):
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


@login_required
def security(request):
    device = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
    events = LoginEvent.objects.filter(user=request.user)[: settings.LOGIN_EVENTS_SHOWN]
    return render(request, "accounts/security.html", {"device": device, "events": events})


class TwoFactorSetupView(View):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.path}")
        return super().dispatch(request, *args, **kwargs)

    def _device(self, request):
        device, _ = TOTPDevice.objects.get_or_create(user=request.user, defaults={"secret": pyotp.random_base32()})
        return device

    def get(self, request):
        device = self._device(request)
        if device.confirmed:
            return redirect("security")
        return self._render(request, device, CodeForm())

    def _render(self, request, device, form):
        uri = pyotp.TOTP(device.secret).provisioning_uri(name=request.user.username, issuer_name="Uptime Monitor")
        return render(
            request, "accounts/two_factor_setup.html", {"form": form, "secret": device.secret, "qr": _qr_svg(uri)}
        )

    def post(self, request):
        device = self._device(request)
        if device.confirmed:
            return redirect("security")
        form = CodeForm(request.POST)
        if form.is_valid() and services.verify_totp(device, form.cleaned_data["code"]):
            device.confirmed = True
            codes = device.new_recovery_codes()
            device.save()
            return render(request, "accounts/recovery_codes.html", {"codes": codes})
        form.add_error("code", "Невірний код. Перевірте час на пристрої й спробуйте ще раз.")
        return self._render(request, device, form)


@login_required
@require_POST
def two_factor_disable(request):
    if not request.user.check_password(request.POST.get("password", "")):
        messages.error(request, "Невірний пароль.")
        return redirect("security")
    TOTPDevice.objects.filter(user=request.user).delete()
    messages.success(request, "Двофакторну автентифікацію вимкнено.")
    return redirect("security")

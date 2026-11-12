import io
import time

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core import signing
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic.edit import FormView

from .forms import CodeForm, LoginForm, ResendForm, SignupForm
from .models import TOTPDevice

User = get_user_model()
CONFIRM_SALT = "email-confirm"
CONFIRM_MAX_AGE = 3 * 24 * 3600
PRE_2FA_MAX_AGE = 300
MAX_2FA_ATTEMPTS = 5
BACKEND = "django.contrib.auth.backends.ModelBackend"


# ---------- email confirmation ----------
def send_confirmation(request, user):
    token = signing.dumps(user.pk, salt=CONFIRM_SALT)
    link = request.build_absolute_uri(reverse("confirm_email", args=[token]))
    send_mail(
        "Підтвердіть email",
        f"Щоб активувати акаунт, відкрийте посилання (діє 3 дні):\n{link}",
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )


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
    try:
        pk = signing.loads(token, salt=CONFIRM_SALT, max_age=CONFIRM_MAX_AGE)
        user = User.objects.get(pk=pk)
    except (signing.BadSignature, User.DoesNotExist):
        return render(request, "registration/confirm_failed.html", status=400)
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    messages.success(request, "Email підтверджено. Тепер можна увійти.")
    return redirect("login")


def resend_confirmation(request):
    """Always answers the same way, so it can't be used to discover which emails exist."""
    form = ResendForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = User.objects.filter(email__iexact=form.cleaned_data["email"], is_active=False).first()
        if user:
            send_confirmation(request, user)
        messages.info(request, "Якщо такий неактивований акаунт існує, ми надіслали лист повторно.")
        return redirect("login")
    return render(request, "registration/resend.html", {"form": form})


# ---------- login with optional TOTP ----------
class TwoFactorLoginView(LoginView):
    form_class = LoginForm

    def form_valid(self, form):
        user = form.get_user()
        device = TOTPDevice.objects.filter(user=user, confirmed=True).first()
        if device is None:
            return super().form_valid(form)
        # Password is correct but the user is NOT logged in yet.
        self.request.session["pre_2fa"] = {
            "user": user.pk,
            "ts": time.time(),
            "next": self.get_redirect_url(),
            "tries": 0,
        }
        return redirect("two_factor_verify")


def _verify_totp(device, code):
    totp = pyotp.TOTP(device.secret)
    code = code.replace(" ", "")
    now = int(time.time())
    for offset in (-1, 0, 1):  # tolerate 30s clock skew
        step = now // 30 + offset
        if step > device.last_used_step and totp.at(step * 30) == code:
            device.last_used_step = step  # one code, one login
            device.save(update_fields=["last_used_step"])
            return True
    return False


def two_factor_verify(request):
    pre = request.session.get("pre_2fa")
    if not pre or time.time() - pre["ts"] > PRE_2FA_MAX_AGE:
        request.session.pop("pre_2fa", None)
        messages.warning(request, "Сесія входу завершилась. Увійдіть знову.")
        return redirect("login")
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        pre["tries"] += 1
        request.session["pre_2fa"] = pre
        if pre["tries"] > MAX_2FA_ATTEMPTS:
            request.session.pop("pre_2fa", None)
            messages.error(request, "Забагато невдалих спроб. Увійдіть знову.")
            return redirect("login")
        user = User.objects.get(pk=pre["user"])
        device = user.totp_device
        code = form.cleaned_data["code"]
        if _verify_totp(device, code) or device.use_recovery_code(code):
            next_url = pre["next"]
            request.session.pop("pre_2fa", None)
            login(request, user, backend=BACKEND)
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
    return render(request, "accounts/security.html", {"device": device})


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
        if form.is_valid() and _verify_totp(device, form.cleaned_data["code"]):
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

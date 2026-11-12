from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from monitors.formkit import BootstrapFormMixin

User = get_user_model()


class SignupForm(BootstrapFormMixin, UserCreationForm):
    email = forms.EmailField(label="Email", required=True)

    class Meta:
        model = User
        fields = ("username", "email")

    # No "email already exists" error here on purpose: it would let anyone probe which addresses
    # have accounts. accounts.services.register() handles a known address silently instead.
    def clean_email(self):
        return self.cleaned_data["email"].strip()


class LoginForm(BootstrapFormMixin, AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        # The stock backend refuses inactive users before the form sees them, so one message
        # covers both cases (and does not reveal which of the two it was).
        "invalid_login": "Невірний логін чи пароль, або акаунт ще не активований "
        "(підтвердіть email за посиланням із листа).",
    }


class ResendForm(BootstrapFormMixin, forms.Form):
    email = forms.EmailField(label="Email")


class CodeForm(BootstrapFormMixin, forms.Form):
    code = forms.CharField(label="Код", max_length=32, strip=True)

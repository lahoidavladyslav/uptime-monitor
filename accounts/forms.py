from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

User = get_user_model()


class SignupForm(UserCreationForm):
    email = forms.EmailField(label="Email", required=True)

    class Meta:
        model = User
        fields = ("username", "email")

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Користувач з таким email уже існує.")
        return email


class LoginForm(AuthenticationForm):
    error_messages = {
        **AuthenticationForm.error_messages,
        # The stock backend refuses inactive users before the form sees them, so one message
        # covers both cases (and does not reveal which of the two it was).
        "invalid_login": "Невірний логін чи пароль, або акаунт ще не активований "
        "(підтвердіть email за посиланням із листа).",
    }


class ResendForm(forms.Form):
    email = forms.EmailField(label="Email")


class CodeForm(forms.Form):
    code = forms.CharField(label="Код", max_length=32, strip=True)

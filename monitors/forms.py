from django import forms
from django.utils import timezone

from .checks import headers_to_lines, parse_header_lines, parse_status_spec
from .formkit import BootstrapFormMixin, HttpURLField
from .models import MaintenanceWindow, Monitor, StatusPage
from .security import blocked_reason


class MonitorForm(BootstrapFormMixin, forms.ModelForm):
    url = HttpURLField(label="URL", max_length=500)

    class Meta:
        model = Monitor
        fields = [
            "name",
            "url",
            "interval_seconds",
            "is_active",
            "is_public",
            "public_title",
            "public_logo_url",
            "http_method",
            "request_headers",
            "request_body",
            "expected_status",
            "expected_text",
        ]
        widgets = {"request_body": forms.Textarea(attrs={"rows": 3})}

    public_logo_url = HttpURLField(label="Логотип (URL)", required=False, max_length=500)
    request_headers = forms.CharField(
        label="Заголовки",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Authorization: Bearer …\nAccept: application/json"}),
        help_text="По одному в рядку: Name: value (до 10).",
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["http_method"].required = False
        if self.instance.pk:
            self.initial["request_headers"] = headers_to_lines(self.instance.request_headers)

    def clean_url(self):
        url = self.cleaned_data["url"]
        reason = blocked_reason(url)
        if reason:
            raise forms.ValidationError(reason)
        return url

    def clean_http_method(self):
        return self.cleaned_data.get("http_method") or "GET"

    def clean_request_headers(self):
        return parse_header_lines(self.cleaned_data.get("request_headers", ""))

    def clean_expected_status(self):
        value = self.cleaned_data.get("expected_status", "").strip()
        parse_status_spec(value)  # raises ValidationError with a friendly message
        return value

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("http_method")
        if method == "HEAD" and cleaned.get("expected_text"):
            self.add_error("expected_text", "HEAD не повертає тіла — пошук тексту неможливий.")
        if method != "POST" and cleaned.get("request_body"):
            self.add_error("request_body", "Тіло запиту доступне лише для POST.")
        url = cleaned.get("url")
        if url:
            # `user` is not a form field, so ModelForm skips the (user, url)
            # uniqueness check; do it here for a friendly message.
            dup = Monitor.objects.filter(user=self.user, url=url).exclude(pk=self.instance.pk)
            if dup.exists():
                self.add_error("url", "Ви вже додали монітор з таким URL.")
        return cleaned


class MaintenanceForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = MaintenanceWindow
        fields = ["starts_at", "ends_at", "reason"]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }
        labels = {"starts_at": "Початок (UTC)", "ends_at": "Кінець (UTC)"}

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("starts_at"), cleaned.get("ends_at")
        if start and end:
            if end <= start:
                raise forms.ValidationError("Кінець має бути пізніше за початок.")
            if end <= timezone.now():
                raise forms.ValidationError("Вікно обслуговування вже минуло.")
            if (end - start).days > 30:
                raise forms.ValidationError("Вікно не може бути довшим за 30 днів.")
        return cleaned


class StatusPageForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = StatusPage
        fields = ["title", "slug", "description", "logo_url", "is_public", "monitors"]
        widgets = {"monitors": forms.CheckboxSelectMultiple}
        help_texts = {"slug": "Сторінка буде за адресою /page/<slug>/"}

    logo_url = HttpURLField(label="Логотип (URL)", required=False, max_length=500)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        # a user can only publish their own monitors
        self.fields["monitors"].queryset = Monitor.objects.filter(user=user)
        self.fields["monitors"].required = False

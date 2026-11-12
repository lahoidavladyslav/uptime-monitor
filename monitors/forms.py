from django import forms

from .models import Monitor, http_url_validator
from .security import blocked_reason


class MonitorForm(forms.ModelForm):
    url = forms.URLField(label="URL", max_length=500, assume_scheme="https", validators=[http_url_validator])

    class Meta:
        model = Monitor
        fields = ["name", "url", "interval_seconds", "is_active", "is_public"]

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_url(self):
        url = self.cleaned_data["url"]
        reason = blocked_reason(url)
        if reason:
            raise forms.ValidationError(reason)
        return url

    def clean(self):
        cleaned = super().clean()
        url = cleaned.get("url")
        if url:
            # `user` is not a form field, so ModelForm skips the (user, url)
            # uniqueness check; do it here for a friendly message.
            dup = Monitor.objects.filter(user=self.user, url=url).exclude(pk=self.instance.pk)
            if dup.exists():
                self.add_error("url", "Ви вже додали монітор з таким URL.")
        return cleaned

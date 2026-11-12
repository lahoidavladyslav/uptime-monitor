"""Small form helpers shared by all apps: Bootstrap classes, visible errors, one-message URL fields."""

from django import forms
from django.contrib import messages

from .models import http_url_validator


class HttpURLField(forms.URLField):
    """http/https only, and ONE error message (the stock field + an extra validator reported it twice)."""

    default_validators = [http_url_validator]

    def __init__(self, **kwargs):
        kwargs.setdefault("assume_scheme", "https")
        super().__init__(**kwargs)


class BootstrapFormMixin:
    """Bootstrap look + red `is-invalid` marking, so a failed submit is impossible to overlook."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                css = "form-check-input"
            elif isinstance(widget, (forms.CheckboxSelectMultiple, forms.RadioSelect)):
                css = ""
            elif isinstance(widget, forms.Select):
                css = "form-select"
            else:
                css = "form-control"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()

    def full_clean(self):
        super().full_clean()
        if self.is_bound:
            for name in self.errors:
                if name in self.fields:
                    widget = self.fields[name].widget
                    widget.attrs["class"] = f"{widget.attrs.get('class', '')} is-invalid".strip()


class FormErrorMessageMixin:
    """CBV mixin: a failed submit also raises a flash alert at the top of the page."""

    error_message = "Не збережено — виправте помилки у формі."

    def form_invalid(self, form):
        messages.error(self.request, self.error_message)
        return super().form_invalid(form)

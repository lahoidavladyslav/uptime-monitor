from datetime import timedelta

from django import forms

MAX_DAYS = 90


class ReportForm(forms.Form):
    period_start = forms.DateField(label="Від", widget=forms.DateInput(attrs={"type": "date"}))
    period_end = forms.DateField(label="До", widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("period_start"), cleaned.get("period_end")
        if start and end:
            if end < start:
                raise forms.ValidationError("Дата «До» не може бути раніше за «Від».")
            if end - start > timedelta(days=MAX_DAYS - 1):
                raise forms.ValidationError(f"Максимальний період — {MAX_DAYS} днів.")
        return cleaned

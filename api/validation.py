"""Monitor validation for the API = the Django form's validation, not a second copy of it."""

import copy
from collections.abc import Mapping
from typing import Any

from monitors.forms import MonitorForm
from monitors.models import Monitor

from .errors import field_errors_from_django


def validate_monitor(user, data: Mapping[str, Any], instance: Monitor | None = None):
    """Returns (cleaned_data, errors); errors is {} on success and cleaned_data is {} on failure.

    Every rule (URL scheme, SSRF guard, status ranges, headers, HEAD+text, (user, url) uniqueness)
    lives in MonitorForm, so the HTML form and the API can never disagree.
    """
    # ModelForm writes cleaned values onto its instance: validate a copy, leave the caller's object alone.
    form = MonitorForm(data, user=user, instance=copy.copy(instance) if instance is not None else None)
    if form.is_valid():
        return form.cleaned_data, {}
    return {}, field_errors_from_django(form.errors)


def save_monitor(user, data: Mapping[str, Any], instance: Monitor | None = None):
    """Create or update through the same MonitorForm. Returns (monitor, errors): errors is {} on success and
    nothing is written on failure."""
    form = MonitorForm(data, user=user, instance=instance)
    if not form.is_valid():
        return None, field_errors_from_django(form.errors)
    monitor = form.save(commit=False)
    monitor.user = user
    monitor.save()  # Monitor.save() generates the public slug once; signals drop the cached public page
    return monitor, {}

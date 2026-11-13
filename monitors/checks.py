"""Pure helpers for check configuration and response evaluation (no I/O)."""

import re

from django.core.exceptions import ValidationError

MAX_BODY_BYTES = 1024 * 1024  # how much of a response we read when searching for text
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def parse_header_lines(text):
    """'Name: value' per line -> dict. Raises ValidationError on junk or dangerous headers."""
    headers = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        name, sep, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not _HEADER_NAME.match(name):
            raise ValidationError(f"Невірний заголовок: «{line[:60]}». Формат: Name: value")
        if name.lower() in FORBIDDEN_HEADERS:
            raise ValidationError(f"Заголовок {name} задавати не можна.")
        if "\r" in value or "\n" in value:
            raise ValidationError("Значення заголовка не може містити переноси рядка.")
        headers[name] = value
    if len(headers) > 10:
        raise ValidationError("Не більше 10 заголовків.")
    return headers


def headers_to_lines(headers):
    return "\n".join(f"{k}: {v}" for k, v in (headers or {}).items())

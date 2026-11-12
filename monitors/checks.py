"""Pure helpers for check configuration and response evaluation (no I/O)."""

import re

from django.core.exceptions import ValidationError

MAX_BODY_BYTES = 1024 * 1024  # how much of a response we read when searching for text
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def parse_status_spec(spec):
    """'200,204,300-399' -> [(200,200),(204,204),(300,399)]. Empty -> [] (means default 2xx/3xx)."""
    ranges = []
    for part in (p.strip() for p in (spec or "").split(",")):
        if not part:
            continue
        m = re.fullmatch(r"(\d{3})(?:\s*-\s*(\d{3}))?", part)
        if not m:
            raise ValidationError(f"«{part}» — невірний формат. Приклад: 200,204,300-399")
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        if not (100 <= lo <= hi <= 599):
            raise ValidationError(f"«{part}» — код має бути в межах 100–599, початок ≤ кінець.")
        ranges.append((lo, hi))
    return ranges


def status_ok(code, spec):
    ranges = parse_status_spec(spec)
    if not ranges:
        return 200 <= code < 400
    return any(lo <= code <= hi for lo, hi in ranges)


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


def evaluate(code, spec, expected_text, body_text):
    """Returns (is_up, error)."""
    if not status_ok(code, spec):
        if parse_status_spec(spec):
            return False, f"Очікувався код {spec.strip()}, отримано {code}"
        return False, f"HTTP {code}"
    if expected_text and expected_text not in (body_text or ""):
        return False, "Очікуваний текст не знайдено у відповіді"
    return True, None

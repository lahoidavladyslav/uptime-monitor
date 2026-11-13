"""Check types as strategies in a registry.

Adding a type = one class + `register(...)`. `check_monitor`/`check_batch` never branch on the type: they ask the
registry for the strategy and call `evaluate_http(...)` (or `evaluate_ssl(...)`). The same `validate_config` runs in
the form, in `Monitor.clean()` and in `Monitor.save()`, so an invalid config cannot reach the database.

Every HTTP type shares the status rule: the response status is checked FIRST (default: 2xx/3xx, or
`expected_statuses`), because the body of an error page proves nothing about the thing being checked.
"""

import json
import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError

MAX_KEYWORD_LENGTH = 200
MAX_PATH_LENGTH = 200
MAX_STATUS_ENTRIES = 20
_RANGE = re.compile(r"^(\d{3})-(\d{3})$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class HttpResponse:
    """What an HTTP check saw. `body` is capped by the reader (1 MiB); `truncated` says it was cut."""

    status_code: int
    body: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class Outcome:
    is_up: bool
    message: str | None = None  # human reason, only when down
    details: dict = field(default_factory=dict)  # stored in CheckResult.details


def _fail(errors: dict[str, str]):
    raise ValidationError({k: [v] for k, v in errors.items()})


def _status_entry_ok(entry) -> bool:
    if isinstance(entry, bool):
        return False
    if isinstance(entry, int):
        return 100 <= entry <= 599
    match = _RANGE.match(entry) if isinstance(entry, str) else None
    return bool(match) and 100 <= int(match.group(1)) <= int(match.group(2)) <= 599


def _status_allowed(code: int, entries: list) -> bool:
    if not entries:
        return 200 <= code < 400
    for entry in entries:
        if isinstance(entry, int):
            if code == entry:
                return True
        else:
            lo, hi = map(int, _RANGE.match(entry).groups())
            if lo <= code <= hi:
                return True
    return False


class CheckType:
    key: str = ""
    label: str = ""
    needs_http = True  # does this type make an HTTP request each interval?
    needs_body = False  # must the response body be read (up to the cap)?
    keys: dict = {}  # allowed config keys -> validator name (documentation + unknown-key rejection)

    # ---- configuration ----
    def clean_config(self, config) -> dict:
        """Validated copy with defaults filled in. Raises ValidationError({field: [msg]})."""
        if not isinstance(config, dict):
            _fail({"config": "must be a JSON object"})
        unknown = set(config) - set(self.keys)
        if unknown:
            _fail({name: f"is not a setting of the '{self.key}' check" for name in sorted(unknown)})
        cleaned = dict(config)
        self._clean(cleaned)
        return cleaned

    def _clean(self, config: dict) -> None:  # filled in by subclasses
        pass

    @staticmethod
    def _clean_statuses(config: dict) -> None:
        if "expected_statuses" not in config:
            return
        entries = config["expected_statuses"]
        if not isinstance(entries, list) or len(entries) > MAX_STATUS_ENTRIES:
            _fail({"expected_statuses": f"must be a list of at most {MAX_STATUS_ENTRIES} codes or ranges"})
        if not all(_status_entry_ok(e) for e in entries):
            _fail({"expected_statuses": "every item must be a code 100-599 or a range like '300-399'"})

    # ---- evaluation ----
    def evaluate_http(self, config: dict, response: HttpResponse) -> Outcome:
        entries = config.get("expected_statuses", [])
        if not _status_allowed(response.status_code, entries):
            message = f"HTTP {response.status_code}" + (f", expected one of {entries}" if entries else "")
            return self._down(response, message)
        return self._evaluate_body(config, response)

    def _evaluate_body(self, config: dict, response: HttpResponse) -> Outcome:
        return Outcome(True, None, {"type": self.key, "status_code": response.status_code})

    def _down(self, response: HttpResponse, message: str, **extra) -> Outcome:
        return Outcome(
            False, message, {"type": self.key, "status_code": response.status_code, "message": message, **extra}
        )

    def evaluate_ssl(self, config: dict, days_left: int) -> Outcome:  # only for ssl_expiry
        raise NotImplementedError


class HttpStatusCheck(CheckType):
    key, label = "http_status", "HTTP-статус"
    keys = {"expected_statuses": "list"}

    def _clean(self, config):
        self._clean_statuses(config)


class KeywordCheck(CheckType):
    key, label = "keyword", "Ключове слово"
    needs_body = True
    keys = {"keyword": "str", "must_contain": "bool", "expected_statuses": "list"}

    def _clean(self, config):
        word = config.get("keyword")
        if not isinstance(word, str) or not 1 <= len(word) <= MAX_KEYWORD_LENGTH:
            _fail({"keyword": f"is required, 1-{MAX_KEYWORD_LENGTH} characters"})
        config.setdefault("must_contain", True)
        if not isinstance(config["must_contain"], bool):
            _fail({"must_contain": "must be true or false"})
        self._clean_statuses(config)

    def _evaluate_body(self, config, response):
        word, must = config["keyword"], config.get("must_contain", True)
        present = word in response.body
        if present == must:
            return Outcome(True, None, {"type": self.key, "status_code": response.status_code})
        if must and response.truncated:
            return self._down(response, f"keyword {word!r} not found in the first part of a truncated body")
        return self._down(response, f"keyword {word!r} {'found' if present else 'not found'}")


class JsonPathCheck(CheckType):
    key, label = "json_path", "Значення в JSON"
    needs_body = True
    keys = {"path": "str", "equals": "scalar", "expected_statuses": "list"}

    def _clean(self, config):
        path = config.get("path")
        if (
            not isinstance(path, str)
            or not 1 <= len(path) <= MAX_PATH_LENGTH
            or not all(_SEGMENT.match(part) for part in path.split("."))
        ):
            _fail({"path": "is required: dotted keys/indexes such as status.db or items.0.name"})
        if "equals" not in config:
            _fail({"equals": "is required (use null to expect JSON null)"})
        if not (config["equals"] is None or isinstance(config["equals"], str | int | float | bool)):
            _fail({"equals": "must be a string, number, true/false or null"})
        self._clean_statuses(config)

    def _evaluate_body(self, config, response):
        path, expected = config["path"], config["equals"]
        try:
            document = json.loads(response.body)
        except ValueError:
            if response.truncated:
                return self._down(response, "the response is larger than the limit: JSON truncated, cannot be read")
            return self._down(response, "the response is not valid JSON")
        found, actual = _walk(document, path)
        if not found:
            return self._down(response, f"path {path!r} not found in the JSON response")
        if _same_json_value(actual, expected):
            return Outcome(True, None, {"type": self.key, "status_code": response.status_code})
        return self._down(response, f"{path} = {actual!r}, expected {expected!r}", actual=actual, expected=expected)


def _walk(node, path: str):
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def _same_json_value(actual, expected) -> bool:
    """JSON equality: Python says True == 1 and 1 == 1.0; JSON distinguishes booleans from numbers."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


class SslExpiryCheck(CheckType):
    key, label = "ssl_expiry", "Термін SSL-сертифіката"
    needs_http = False  # checked once a day by its own task, not every interval
    keys = {"min_days_left": "int"}

    def _clean(self, config):
        config.setdefault("min_days_left", 14)
        days = config["min_days_left"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            _fail({"min_days_left": "must be a whole number of days, 1-365"})

    def evaluate_http(self, config, response):
        raise NotImplementedError("ssl_expiry makes no HTTP check")

    def evaluate_ssl(self, config, days_left):
        minimum = config["min_days_left"]
        details = {"type": self.key, "days_left": days_left, "min_days_left": minimum}
        if days_left >= minimum:
            return Outcome(True, None, details)
        message = f"certificate expires in {days_left} days, need at least {minimum}"
        return Outcome(False, message, {**details, "message": message})


REGISTRY: dict[str, CheckType] = {}


def register(strategy: CheckType) -> CheckType:
    REGISTRY[strategy.key] = strategy
    return strategy


for _strategy in (HttpStatusCheck(), KeywordCheck(), JsonPathCheck(), SslExpiryCheck()):
    register(_strategy)

CHOICES = [(s.key, s.label) for s in REGISTRY.values()]


def get(kind: str) -> CheckType:
    try:
        return REGISTRY[kind]
    except KeyError:
        raise ValidationError({"check_type": [f"unknown check type {kind!r}"]}) from None


def validate_config(kind: str, config) -> dict:
    return get(kind).clean_config(config)


def evaluate_http(kind: str, config: dict, response: HttpResponse) -> Outcome:
    return get(kind).evaluate_http(config, response)

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from . import crypto, signals  # noqa: F401

        crypto.validate_configuration()  # a malformed/missing key stops startup, not the first 2FA login

import hashlib
import secrets

from django.conf import settings
from django.db import models


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


class TOTPDevice(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="totp_device")
    # Plain secret: TOTP needs it in clear to verify codes. Protect the DB; encrypting at rest
    # (e.g. Fernet with a key outside the DB) is the next step, see README.
    secret = models.CharField(max_length=64)
    confirmed = models.BooleanField(default=False)
    last_used_step = models.BigIntegerField(default=0)  # replay protection
    recovery_hashes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    def new_recovery_codes(self, n=8):
        """Returns plaintext codes ONCE; only their hashes are stored."""
        codes = [secrets.token_hex(5) for _ in range(n)]
        self.recovery_hashes = [_hash(c) for c in codes]
        return codes

    def use_recovery_code(self, code):
        h = _hash(code.strip().lower())
        if h in self.recovery_hashes:
            self.recovery_hashes = [x for x in self.recovery_hashes if x != h]
            self.save(update_fields=["recovery_hashes"])
            return True
        return False


class LoginEvent(models.Model):
    """One row per sign-in attempt against a real account (shown to its owner as "recent activity").

    Attempts with an unknown username are NOT stored: they are bot noise that would grow the table
    without bound, and the "username" is often a password pasted into the wrong box. They only reach
    the structured log, without the name.
    """

    KIND_CHOICES = [
        ("login", "Успішний вхід"),
        ("failed", "Невірний пароль"),
        ("two_factor_failed", "Невірний код 2FA"),
        ("two_factor_locked", "2FA заблоковано після спроб"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="login_events")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="loginevent_user_time_idx"),
            models.Index(fields=["created_at"], name="loginevent_time_idx"),  # retention cleanup
        ]

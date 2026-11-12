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

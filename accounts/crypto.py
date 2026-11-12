"""Encryption at rest for TOTP secrets (Fernet = AES-128-CBC + HMAC-SHA256, random IV per value).

Keys come from TOTP_ENCRYPTION_KEYS (newest first). The first key encrypts, every key can decrypt,
so rotating is: prepend a new key, deploy, (optionally) re-save rows, later drop the old key.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PREFIX = "fernet:"


def derive_dev_key(secret_key: str) -> str:
    """Development/test only: a stable key derived from SECRET_KEY. Production must set its own."""
    return base64.urlsafe_b64encode(hashlib.sha256(b"totp-secret:" + secret_key.encode()).digest()).decode()


def _cipher() -> MultiFernet:
    keys = settings.TOTP_ENCRYPTION_KEYS
    if not keys:
        raise ImproperlyConfigured("TOTP_ENCRYPTION_KEYS is empty")
    try:
        return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "TOTP_ENCRYPTION_KEYS must hold url-safe base64 32-byte Fernet keys "
            '(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")'
        ) from exc


def validate_configuration() -> None:
    _cipher()


def encrypt(plain: str) -> str:
    return PREFIX + _cipher().encrypt(plain.encode()).decode()


def decrypt(stored: str) -> str:
    """Legacy rows written before encryption existed have no prefix and are returned as they are
    (the 0003 data migration encrypts them; this keeps a half-migrated database working)."""
    if not stored.startswith(PREFIX):
        return stored
    try:
        return _cipher().decrypt(stored[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "A TOTP secret cannot be decrypted: TOTP_ENCRYPTION_KEYS is missing the key it was written with, "
            "or the stored value was modified."
        ) from exc

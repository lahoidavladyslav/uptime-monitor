from django.db import models

from . import crypto


class EncryptedTextField(models.TextField):
    """Stored as `fernet:<token>`; Python code sees the plain string.

    Not searchable (the ciphertext differs on every write), which is fine for a per-user secret.
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return None if value is None else crypto.encrypt(value)

    def from_db_value(self, value, expression, connection):
        return None if value is None else crypto.decrypt(value)

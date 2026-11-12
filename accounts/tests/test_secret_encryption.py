import importlib

import pyotp
import pytest
from cryptography.fernet import Fernet
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from accounts import services
from accounts.models import TOTPDevice


def raw_secret(pk):
    with connection.cursor() as cur:
        cur.execute("SELECT secret FROM accounts_totpdevice WHERE id = %s", [pk])
        return cur.fetchone()[0]


def set_raw(pk, value):
    with connection.cursor() as cur:
        cur.execute("UPDATE accounts_totpdevice SET secret = %s WHERE id = %s", [value, pk])


@pytest.fixture
def device(user):
    return TOTPDevice.objects.create(user=user, secret=pyotp.random_base32(), confirmed=True)


def test_secret_is_not_stored_in_clear(user):
    plain = pyotp.random_base32()
    d = TOTPDevice.objects.create(user=user, secret=plain, confirmed=True)
    stored = raw_secret(d.pk)
    assert stored.startswith("fernet:") and plain not in stored


def test_the_model_still_returns_the_plain_secret_and_codes_verify(device):
    plain = pyotp.random_base32()
    device.secret = plain
    device.save()
    device.refresh_from_db()
    assert device.secret == plain
    assert services.verify_totp(device, pyotp.TOTP(plain).now())


def test_encrypting_twice_gives_different_ciphertext(user):
    a = TOTPDevice.objects.create(user=user, secret="JBSWY3DPEHPK3PXP", confirmed=True)
    first = raw_secret(a.pk)
    a.save()
    assert raw_secret(a.pk) != first  # random IV: equal secrets are not recognisable in the DB


def test_a_legacy_plaintext_row_still_reads(device):
    set_raw(device.pk, "LEGACYPLAINSECRET")
    device.refresh_from_db()
    assert device.secret == "LEGACYPLAINSECRET"


def test_data_migration_encrypts_legacy_rows_and_is_idempotent(device):
    migration = importlib.import_module("accounts.migrations.0003_encrypt_totp_secrets")
    set_raw(device.pk, "LEGACYPLAINSECRET")
    migration.encrypt_existing(apps, None)
    assert raw_secret(device.pk).startswith("fernet:") and "LEGACYPLAINSECRET" not in raw_secret(device.pk)
    migration.encrypt_existing(apps, None)
    device.refresh_from_db()
    assert device.secret == "LEGACYPLAINSECRET"


def test_key_rotation_old_data_is_readable_and_new_writes_use_the_new_key(device, settings):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    settings.TOTP_ENCRYPTION_KEYS = [old]
    device.secret = "ROTATEME"
    device.save()
    settings.TOTP_ENCRYPTION_KEYS = [new, old]  # newest first
    device.refresh_from_db()
    assert device.secret == "ROTATEME"
    device.save()  # re-encrypted with `new`
    settings.TOTP_ENCRYPTION_KEYS = [new]
    device.refresh_from_db()
    assert device.secret == "ROTATEME"


def test_losing_the_key_fails_loudly_not_silently(device, settings):
    settings.TOTP_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    with pytest.raises(ImproperlyConfigured, match="TOTP_ENCRYPTION_KEYS"):
        TOTPDevice.objects.get(pk=device.pk)


def test_tampered_ciphertext_is_rejected(device):
    set_raw(device.pk, raw_secret(device.pk)[:-4] + "AAAA")
    with pytest.raises(ImproperlyConfigured):
        TOTPDevice.objects.get(pk=device.pk)

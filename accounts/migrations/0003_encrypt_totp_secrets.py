from django.db import migrations

import accounts.fields


def encrypt_existing(apps, schema_editor):
    """Re-save every device: values are read tolerantly (legacy plaintext or already encrypted) and
    written encrypted. Safe to run repeatedly."""
    TOTPDevice = apps.get_model("accounts", "TOTPDevice")
    for device in TOTPDevice.objects.all().iterator():
        device.save(update_fields=["secret"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_loginevent")]

    operations = [
        migrations.AlterField(model_name="totpdevice", name="secret", field=accounts.fields.EncryptedTextField()),
        migrations.RunPython(encrypt_existing, migrations.RunPython.noop),
    ]

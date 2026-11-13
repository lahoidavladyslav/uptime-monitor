from django.db import migrations, models

NEW_THRESHOLDS = (14, 7, 1, 0)


def level_to_notified(level):
    """The old column held the SMALLEST threshold already mailed (of 14/3/0). Everything at or above it counts as
    notified under the new 14/7/1/0 thresholds, so nobody is mailed twice for the same certificate."""
    return [] if level is None else [t for t in NEW_THRESHOLDS if t >= level]


def forwards(apps, schema_editor):
    Monitor = apps.get_model("monitors", "Monitor")
    for m in Monitor.objects.exclude(ssl_alerted_level__isnull=True).iterator():
        m.ssl_notified = level_to_notified(m.ssl_alerted_level)
        m.save(update_fields=["ssl_notified"])


def backwards(apps, schema_editor):
    Monitor = apps.get_model("monitors", "Monitor")
    for m in Monitor.objects.exclude(ssl_notified=[]).iterator():
        m.ssl_alerted_level = min(m.ssl_notified)
        m.save(update_fields=["ssl_alerted_level"])


class Migration(migrations.Migration):
    dependencies = [("monitors", "0013_drop_legacy_expected")]

    operations = [
        migrations.AddField(
            model_name="monitor", name="ssl_notified", field=models.JSONField(blank=True, default=list)
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="monitor", name="ssl_alerted_level"),
    ]

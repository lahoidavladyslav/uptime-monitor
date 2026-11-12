import django.db.models.deletion
from django.db import migrations, models

# Existing hourly rows have no min/max yet: fill them from raw checks that still exist (a one-off
# grouped UPDATE). Hours whose raw rows were already cleaned up keep NULL, which readers ignore.
FILL_MIN_MAX = """
UPDATE monitors_checkrolluphourly r
SET min_response_ms = s.lo, max_response_ms = s.hi
FROM (
    SELECT monitor_id,
           date_trunc('hour', checked_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour,
           min(response_time_ms) AS lo, max(response_time_ms) AS hi
    FROM monitors_checkresult
    GROUP BY 1, 2
) s
WHERE r.monitor_id = s.monitor_id AND r.hour = s.hour
"""


class Migration(migrations.Migration):
    dependencies = [("monitors", "0005_subscriber")]

    operations = [
        migrations.RenameModel("HourlyStat", "CheckRollupHourly"),
        migrations.RenameField("CheckRollupHourly", "total", "checks_total"),
        migrations.RenameField("CheckRollupHourly", "up", "checks_up"),
        migrations.RenameField("CheckRollupHourly", "rt_sum", "sum_response_ms"),
        migrations.RenameField("CheckRollupHourly", "rt_count", "response_count"),
        migrations.AddField(
            "CheckRollupHourly", "min_response_ms", models.PositiveIntegerField(blank=True, null=True)
        ),
        migrations.AddField(
            "CheckRollupHourly", "max_response_ms", models.PositiveIntegerField(blank=True, null=True)
        ),
        migrations.AlterField(
            "CheckRollupHourly",
            "monitor",
            models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="rollups", to="monitors.monitor"
            ),
        ),
        migrations.RunSQL(FILL_MIN_MAX, migrations.RunSQL.noop),
    ]

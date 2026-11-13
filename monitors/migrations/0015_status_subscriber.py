from django.db import migrations, models
from django.db.models import Q
from django.db.models.functions import Lower


class Migration(migrations.Migration):
    dependencies = [("monitors", "0014_ssl_notified")]

    operations = [
        migrations.RenameModel("Subscriber", "StatusSubscriber"),  # keeps every existing row
        migrations.AddField(
            model_name="statussubscriber",
            name="unsubscribed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # uniqueness per address now ignores case, whoever writes the row
        migrations.RemoveConstraint(model_name="statussubscriber", name="uniq_subscriber_page_email"),
        migrations.RemoveConstraint(model_name="statussubscriber", name="uniq_subscriber_monitor_email"),
        migrations.AddConstraint(
            model_name="statussubscriber",
            constraint=models.UniqueConstraint(
                "status_page", Lower("email"), condition=Q(status_page__isnull=False), name="uniq_subscriber_page_email"
            ),
        ),
        migrations.AddConstraint(
            model_name="statussubscriber",
            constraint=models.UniqueConstraint(
                "monitor", Lower("email"), condition=Q(monitor__isnull=False), name="uniq_subscriber_monitor_email"
            ),
        ),
    ]

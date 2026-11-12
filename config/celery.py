import os

from celery import Celery
from celery.signals import task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from config.observability import task_id_var  # noqa: E402

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@task_prerun.connect
def _bind_task_id(task_id=None, **kwargs):
    task_id_var.set(task_id)  # every log line from the task now carries task_id


@task_postrun.connect
def _unbind_task_id(**kwargs):
    task_id_var.set(None)

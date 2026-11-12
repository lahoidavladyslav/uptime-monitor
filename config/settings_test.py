from .settings import *  # noqa: F403
from .settings import BASE_DIR

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MEDIA_ROOT = BASE_DIR / ".test_media"

# The local Postgres may allow very few connections; never keep idle ones around in tests.
DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405

import os
from pathlib import Path

import dj_database_url
from celery.schedules import crontab

from accounts.crypto import derive_dev_key
from config.observability import build_logging

BASE_DIR = Path(__file__).resolve().parent.parent

# DJANGO_ENV=production turns on strict, fail-fast configuration (see docker-compose.prod.yml).
PRODUCTION = os.environ.get("DJANGO_ENV") == "production"
DEBUG = not PRODUCTION and os.environ.get("DEBUG", "1") == "1"

if PRODUCTION:
    # No insecure fallbacks in production: refuse to start instead of running unsafely.
    SECRET_KEY = os.environ["SECRET_KEY"]
    ALLOWED_HOSTS = [h for h in os.environ["ALLOWED_HOSTS"].split(",") if h]
    # Fernet keys, newest first (the first one encrypts, all of them decrypt: that is key rotation).
    TOTP_ENCRYPTION_KEYS = [k.strip() for k in os.environ["TOTP_ENCRYPTION_KEYS"].split(",") if k.strip()]
else:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-key-change-me")
    ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")
    TOTP_ENCRYPTION_KEYS = [k.strip() for k in os.environ.get("TOTP_ENCRYPTION_KEYS", "").split(",") if k.strip()] or [
        derive_dev_key(SECRET_KEY)
    ]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "api",
    "monitors",
    "reports",
]

MIDDLEWARE = [
    "config.observability.RequestLogMiddleware",  # outermost: times everything, binds the request id
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("DATABASE_URL", "postgres://localhost/uptime"),
        conn_max_age=60,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "uk"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if PRODUCTION
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

from django.contrib.messages import constants as message_constants  # noqa: E402

# Bootstrap calls the red alert "danger"; Django's default tag "error" has no such class,
# so every error flash message used to render unstyled.
MESSAGE_TAGS = {message_constants.ERROR: "danger", message_constants.DEBUG: "secondary"}

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
# SMTP settings are read in every environment, so real mail can be tried in dev too (set EMAIL_BACKEND
# to the smtp backend plus the EMAIL_* variables). Without them mail is printed to the console.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "uptime@example.com")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")  # where the SPA lives (links in emails)
# Where the Django-rendered pages (public status pages, subscription links) are reachable from outside.
# In production nginx serves both on one origin, so it defaults to FRONTEND_URL there.
SITE_URL = os.environ.get("SITE_URL", FRONTEND_URL if PRODUCTION else "http://localhost:8000").rstrip("/")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("CACHE_REDIS_URL", REDIS_URL),
    }
}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULE = {
    "schedule-checks": {
        "task": "monitors.tasks.schedule_checks",
        "schedule": 60.0,
    },
    "build-rollups": {
        "task": "monitors.tasks.build_rollups",
        "schedule": crontab(minute="*/5"),
    },
    "ssl-expiry-checks": {
        "task": "monitors.tasks.schedule_ssl_checks",
        "schedule": crontab(hour=4, minute=0),
    },
    "cleanup-unconfirmed-subscribers": {
        "task": "monitors.tasks.cleanup_unconfirmed_subscribers",
        "schedule": crontab(hour=3, minute=15),
    },
    "cleanup-login-events": {
        "task": "accounts.tasks.cleanup_login_events",
        "schedule": crontab(hour=3, minute=30),
    },
    "cleanup-old-checks": {
        "task": "monitors.tasks.cleanup_old_checks",
        "schedule": crontab(hour=3, minute=0),
    },
}

# Uptime-monitor knobs
ALLOW_PRIVATE_TARGETS = os.environ.get("ALLOW_PRIVATE_TARGETS", "0") == "1"
CHECK_NOW_COOLDOWN_SECONDS = 10
MAX_IN_PROGRESS_REPORTS = 3
CHECK_TIMEOUT_SECONDS = 10
CHECK_RETENTION_DAYS = 7  # raw checks; everything older is served from the hourly rollups
ROLLUP_RETENTION_DAYS = 365
CLEANUP_BATCH_SIZE = 5000
LOGIN_EVENT_RETENTION_DAYS = 90
SUBSCRIBE_LIMIT_PER_HOUR = 10  # per client IP
SUBSCRIBE_RESEND_COOLDOWN_MINUTES = 10  # per address
MAX_SUBSCRIBERS_PER_TARGET = 5000
LOGIN_EVENTS_SHOWN = 50
PUBLIC_STATUS_CACHE_SECONDS = 60

# ---------------------------- queues ----------------------------
# Slow report generation, mail and the check loop must not starve each other.
CELERY_TASK_DEFAULT_QUEUE = "checks"
CELERY_TASK_ROUTES = {
    "monitors.tasks.send_incident_notification": {"queue": "notifications"},
    "monitors.tasks.send_ssl_alert": {"queue": "notifications"},
    "monitors.tasks.notify_subscribers": {"queue": "notifications"},
    "monitors.tasks.send_subscriber_email": {"queue": "notifications"},
    "monitors.tasks.send_subscription_confirmation": {"queue": "notifications"},
    "reports.tasks.generate_report": {"queue": "reports"},
}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # don't let one worker hoard long jobs
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# ---------------------------- observability ----------------------------
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "")  # empty -> /metrics is disabled (404)
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment="production" if PRODUCTION else "development",
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False,  # no emails/IPs/cookies in error reports
    )

# ---------------------------- production hardening ----------------------------
if PRODUCTION:
    HTTPS = os.environ.get("HTTPS", "1") == "1"  # TLS is terminated in front of nginx
    CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o]
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = HTTPS
    # nginx/the load balancer terminates TLS and sets X-Forwarded-Proto; Django enforces the redirect.
    # /healthz/ is exempt because orchestrator probes arrive over plain HTTP inside the network.
    SECURE_SSL_REDIRECT = HTTPS
    SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
    SECURE_HSTS_SECONDS = 31536000 if HTTPS else 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = HTTPS
    # Preload is effectively irreversible, so it is opt-in (HSTS_PRELOAD=1), not a default.
    SECURE_HSTS_PRELOAD = HTTPS and os.environ.get("HSTS_PRELOAD") == "1"
    if not SECURE_HSTS_PRELOAD:
        SILENCED_SYSTEM_CHECKS = ["security.W021"]
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")

# Swagger UI and the schema describe every endpoint: not for strangers in production (API_DOCS=1 re-enables).
API_DOCS_ENABLED = os.environ.get("API_DOCS", "0" if PRODUCTION else "1") == "1"

# ---------------------------- logging ----------------------------
# JSON lines in production (what log shippers want), readable text in development. LOG_FORMAT overrides.
LOG_FORMAT = os.environ.get("LOG_FORMAT", "json" if PRODUCTION else "plain")
LOGGING = build_logging(json_format=LOG_FORMAT == "json", level=os.environ.get("LOG_LEVEL", "INFO"))
# only nginx (which overwrites X-Real-IP) may tell us who the client is
TRUST_PROXY_HEADERS = PRODUCTION
# the Celery worker must not replace our log config with its own
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG

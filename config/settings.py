import os
from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY", default="django-insecure-change-me")
DEBUG = config("DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1").split(",")
ALLOWED_HOSTS += ["*"]  # Railway/Render provide dynamic hostnames

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "django_celery_results",
    "django_celery_beat",
    "channels",
    # Local apps
    "apps.profiles",
    "apps.sessions",
    "apps.campaigns",
    "apps.autoreply",
    "apps.messaging",
    "apps.contacts",
    "apps.realtime",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
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
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION  = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="whatsapp_automation"),
        "USER": config("DB_USER", default="postgres"),
        "PASSWORD": config("DB_PASSWORD", default="postgres"),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# JWT Settings
from datetime import timedelta
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=24),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
}

# CORS
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

# Celery
from kombu import Exchange, Queue
from shared.constants.queues import (
    CAMPAIGN_QUEUE,
    CAMPAIGN_TASKS,
    DEFAULT_QUEUE,
    INCOMING_QUEUE,
    INCOMING_TASKS,
    PROFILE_TASKS,
    QUEUE_NAMES,
    RETRY_QUEUE,
)

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default="django-db")
CELERY_CACHE_BACKEND = "django-cache"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_DEFAULT_QUEUE = DEFAULT_QUEUE
CELERY_TASK_CREATE_MISSING_QUEUES = False
CELERY_TASK_QUEUES = tuple(
    Queue(queue_name, Exchange(queue_name), routing_key=queue_name)
    for queue_name in QUEUE_NAMES
)
CELERY_TASK_ROUTES = {
    **{task_name: {"queue": CAMPAIGN_QUEUE, "routing_key": CAMPAIGN_QUEUE} for task_name in CAMPAIGN_TASKS},
    **{task_name: {"queue": INCOMING_QUEUE, "routing_key": INCOMING_QUEUE} for task_name in INCOMING_TASKS},
    **{task_name: {"queue": DEFAULT_QUEUE, "routing_key": DEFAULT_QUEUE} for task_name in PROFILE_TASKS},
    "*.retry": {"queue": RETRY_QUEUE, "routing_key": RETRY_QUEUE},
    "*.retries": {"queue": RETRY_QUEUE, "routing_key": RETRY_QUEUE},
}
CELERY_BEAT_SCHEDULE = {
    "sync-gologin-profiles-every-3-minutes": {
        "task": "profiles.sync_gologin_profiles",
        "schedule": 180.0,
    },
    # Heartbeat stale window = 15 s (3× the 5 s extension heartbeat interval).
    # Running every 15 s keeps DB/frontend in sync with actual runtime state.
    "check-profile-heartbeats-every-15s": {
        "task": "profiles.check_profile_heartbeats",
        "schedule": 15.0,
    },
    # Release phantom locks: dead PIDs, launch timeouts, cross-process disconnects.
    "cleanup-stale-runtimes-every-10s": {
        "task": "profiles.cleanup_stale_runtimes",
        "schedule": 10.0,
    },
    "reconcile-stale-messages-every-minute": {
        "task": "messaging.reconcile_stale_messages",
        "schedule": 60.0,
    },
    "send-followup-messages-every-hour": {
        "task": "autoreply.send_followup_messages",
        "schedule": 3600.0,
    },
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("REDIS_CACHE_URL", default="redis://127.0.0.1:6379/1"),
    }
}
SESSION_ACTION_LOCK_TTL = config("SESSION_ACTION_LOCK_TTL", default=300, cast=int)

# GoLogin
GOLOGIN_API_TOKEN = config("GOLOGIN_API_TOKEN", default="")
GOLOGIN_API_URL   = config("GOLOGIN_API_URL", default="https://api.gologin.com")

# URL of this Django server as seen from inside the GoLogin browser (localhost by default)
BACKEND_URL = config("BACKEND_URL", default="http://127.0.0.1:8000")

# Claude AI (disabled — using OpenAI instead)
CLAUDE_API_KEY = config("CLAUDE_API_KEY", default="")

# OpenAI (optional — paid AI)
OPENAI_API_KEY = config("OPENAI_API_KEY", default="")

# Groq (fast fallback for replies and personalization)
GROQ_API_KEY = config("GROQ_API_KEY", default="")
GROQ_MODEL = config("GROQ_MODEL", default="llama-3.1-8b-instant")

# Google Gemini (recommended — free tier 1500 req/day, no credit card needed)
# Get free key at: https://aistudio.google.com/app/apikey
GEMINI_API_KEY = config("GEMINI_API_KEY", default="")

# Webhook — public endpoint for adding contacts from external sources
WEBHOOK_SECRET_KEY = config("WEBHOOK_SECRET_KEY", default="change-this-secret")
WEBHOOK_USER_ID = config("WEBHOOK_USER_ID", default=None, cast=lambda v: int(v) if v else None)

# Django Channels — Redis is already running for Celery, so use RedisChannelLayer.
# This allows Celery tasks (follow-ups, etc.) to push messages to WebSocket consumers
# across process boundaries, which InMemoryChannelLayer cannot do.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [("127.0.0.1", 6379)],
        },
    },
}

# API Docs
SPECTACULAR_SETTINGS = {
    "TITLE": "WhatsApp Automation API",
    "DESCRIPTION": "API for managing GoLogin profiles and automating WhatsApp Web messaging",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "file": {
            # Windows cannot reliably rotate this file while Daphne/Celery
            # processes have it open. Use plain append logging locally.
            "class": "logging.FileHandler",
            "filename": BASE_DIR / "django.log",
            "formatter": "verbose",
            "encoding": "utf-8",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "utils": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}

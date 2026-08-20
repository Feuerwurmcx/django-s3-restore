"""Minimale Django-Settings fuer die Tests (kein Projekt noetig)."""
import os

import django
from django.conf import settings

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test-only",
        ALLOWED_HOSTS=["testserver", "localhost"],
        USE_TZ=True,
        TIME_ZONE="Europe/Berlin",
        ROOT_URLCONF="tests.urls",
        INSTALLED_APPS=[
            "django.contrib.admin",
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.staticfiles",
            "s3restore",
        ],
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.middleware.common.CommonMiddleware",
            "django.middleware.csrf.CsrfViewMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        TEMPLATES=[{
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {"context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]},
        }],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                               "NAME": ":memory:"}},
        DEFAULT_AUTO_FIELD="django.db.models.AutoField",
        STATIC_URL="/static/",
        STORAGES={
            "default": {
                "BACKEND": "s3restore.storage.VersionedS3Storage",
                "OPTIONS": {
                    "bucket_name": "garten-backup",
                    "region_name": "eu-central-1",
                    "location": "media",
                    "file_overwrite": True,
                },
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        },
    )
    django.setup()

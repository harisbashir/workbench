"""
Workbench settings.

Zero-configuration by design: everything Workbench stores — database, uploaded
files, backups and its own generated secrets — lives in one data folder
(WORKBENCH_DATA_DIR, `/data` in the container). Environment variables are
optional overrides; see .env.example.
"""
import json
import os
import secrets as _secrets
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    value = os.environ.get(name)
    return default if value in (None, "") else value


def env_bool(name, default=False):
    return str(env(name, default)).lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [x.strip() for x in str(env(name, default)).split(",") if x.strip()]


DEBUG = env_bool("WORKBENCH_DEBUG", False)
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

# --- The data folder -------------------------------------------------------
if TESTING:
    DATA_DIR = Path(tempfile.mkdtemp(prefix="workbench-test-"))
else:
    DATA_DIR = Path(env("WORKBENCH_DATA_DIR", BASE_DIR / "data")).resolve()
DB_DIR = DATA_DIR / "db"
FILES_DIR = DATA_DIR / "files"
BACKUP_DIR = DATA_DIR / "backups"
for _d in (DATA_DIR, DB_DIR, FILES_DIR, BACKUP_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _load_or_create_secrets():
    """Secrets are generated on first start and kept in the data folder.

    Back up the data folder and you back up the keys with it; environment
    variables still win if you prefer to manage secrets yourself.
    """
    path = DATA_DIR / "secrets.json"
    data = {}
    if path.exists():
        data = json.loads(path.read_text())
    changed = False
    for key, maker in {
        "secret_key": lambda: _secrets.token_urlsafe(50),
        "field_key": lambda: __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode(),
        "setup_token": lambda: _secrets.token_hex(4).upper(),
    }.items():
        if not data.get(key):
            data[key] = maker()
            changed = True
    if changed:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    return data


GENERATED = _load_or_create_secrets()
SECRET_KEY = env("WORKBENCH_SECRET_KEY", GENERATED["secret_key"])
FIELD_KEY = env("WORKBENCH_FIELD_KEY", GENERATED["field_key"])
SETUP_TOKEN = GENERATED["setup_token"]

# --- Domain / HTTPS ------------------------------------------------------------
# Set WORKBENCH_DOMAIN (e.g. workbench.example.com) when running with the HTTPS
# profile. Without it Workbench also works on a plain IP/port, e.g. inside a VPN.
DOMAIN = env("WORKBENCH_DOMAIN", "")
HTTPS = env_bool("WORKBENCH_HTTPS", bool(DOMAIN))
if DOMAIN:
    ALLOWED_HOSTS = [DOMAIN, "localhost", "127.0.0.1"] + env_list("WORKBENCH_ALLOWED_HOSTS")
    CSRF_TRUSTED_ORIGINS = [f"https://{DOMAIN}"] + env_list("WORKBENCH_CSRF_TRUSTED_ORIGINS")
else:
    ALLOWED_HOSTS = env_list("WORKBENCH_ALLOWED_HOSTS", "*")
    CSRF_TRUSTED_ORIGINS = env_list("WORKBENCH_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "apps.accounts",
    "apps.core",
    "apps.projects",
    "apps.chat",
    "apps.files",
    "apps.inventory",
    "apps.production",
    "apps.integrations",
    "apps.timesheets",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.FirstRunSetupMiddleware",
    "apps.accounts.middleware.MFARequiredMiddleware",
    "apps.accounts.middleware.UserTimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

try:  # WhiteNoise serves CSS/JS efficiently from the same container.
    import whitenoise  # noqa: F401
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
    if not DEBUG and not TESTING:
        STORAGES = {
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
        }
except ImportError:
    pass

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
                "apps.core.context_processors.workbench",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Database ------------------------------------------------------------------
# SQLite in the data folder by default (plenty for a team of dozens, and backups
# are a single file). Set WORKBENCH_DB_NAME etc. to use PostgreSQL instead.
if env("WORKBENCH_DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("WORKBENCH_DB_NAME"),
            "USER": env("WORKBENCH_DB_USER", ""),
            "PASSWORD": env("WORKBENCH_DB_PASSWORD", ""),
            "HOST": env("WORKBENCH_DB_HOST", "localhost"),
            "PORT": env("WORKBENCH_DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DB_DIR / "workbench.sqlite3",
            "OPTIONS": {
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;",
                "transaction_mode": "IMMEDIATE",
            },
        }
    }

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

WORKBENCH_REQUIRE_MFA = env_bool("WORKBENCH_REQUIRE_MFA", True)
LOGIN_MAX_ATTEMPTS = int(env("WORKBENCH_LOGIN_MAX_ATTEMPTS", 5))
LOGIN_LOCKOUT_MINUTES = int(env("WORKBENCH_LOGIN_LOCKOUT_MINUTES", 15))

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("WORKBENCH_TIME_ZONE", "UTC")
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploaded files live in <data>/files and are only served through
# permission-checked views, never directly.
MEDIA_URL = "/media/"
MEDIA_ROOT = FILES_DIR
MAX_UPLOAD_MB = int(env("WORKBENCH_MAX_UPLOAD_MB", 200))
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_PERMISSIONS = 0o640

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Session & cookie hardening -------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = int(env("WORKBENCH_SESSION_HOURS", 12)) * 3600
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

if HTTPS:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("WORKBENCH_SSL_REDIRECT", True)
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # the container's own health check talks plain HTTP
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

EMAIL_TIMEOUT = 15
# HSTS preload is a one-way, domain-wide decision; leave it to the domain owner.
SILENCED_SYSTEM_CHECKS = ["security.W021"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "workbench": {"handlers": ["console"], "level": "INFO"},
        "django.security": {"handlers": ["console"], "level": "WARNING"},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}

"""Symmetric encryption for sensitive fields (e.g. MFA secrets).

The key comes from WORKBENCH_FIELD_KEY if set, otherwise it is derived from
SECRET_KEY. Rotating SECRET_KEY without setting WORKBENCH_FIELD_KEY will make
existing MFA enrolments unreadable, so set a dedicated key in production.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = os.environ.get("WORKBENCH_FIELD_KEY")
    if not key:
        digest = hashlib.sha256(("workbench-field:" + settings.SECRET_KEY).encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""

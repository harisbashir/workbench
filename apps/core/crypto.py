"""Symmetric encryption for sensitive fields (2FA secrets, SMTP and webhook secrets).

The key is generated on first start and stored in <data>/secrets.json
(or set WORKBENCH_FIELD_KEY). Keep it with your backups: without it,
encrypted values can't be read.
"""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    return Fernet(settings.FIELD_KEY.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def can_decrypt(token: str) -> bool:
    try:
        _fernet().decrypt(token.encode())
        return True
    except (InvalidToken, ValueError):
        return False


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""

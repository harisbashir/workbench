"""Shared helpers for tests."""
import pyotp
from django.test import Client

from apps.accounts.models import User


def make_user(username, role=User.Role.ENGINEER, **extra):
    user = User.objects.create_user(username=username, password="Correct-horse-battery-9", role=role, **extra)
    user.mfa_secret = pyotp.random_base32()
    user.mfa_enabled = True
    user.save()
    return user


def signed_in(user):
    """A test client that is fully signed in, including two-factor verification."""
    c = Client()
    c.force_login(user)
    session = c.session
    session["mfa_verified"] = True
    session.save()
    return c

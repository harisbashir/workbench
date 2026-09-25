"""Create the first administrator and print a one-time link to set their password.

    python manage.py create_admin --username haris --first-name Haris --email you@example.com
"""
from django.contrib.auth.tokens import default_token_generator
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import User
from apps.core.models import SiteSettings


class Command(BaseCommand):
    help = "Create an administrator account and print a one-time link to set its password."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")
        parser.add_argument("--email", default="")
        parser.add_argument("--time-zone", default="America/Toronto")

    def handle(self, *args, **o):
        if User.objects.filter(username__iexact=o["username"]).exists():
            raise CommandError(f"User {o['username']} already exists.")
        user = User(username=o["username"], first_name=o["first_name"], last_name=o["last_name"], email=o["email"],
                    role=User.Role.ADMIN, is_staff=True, is_superuser=True, time_zone=o["time_zone"])
        user.set_unusable_password()
        user.save()
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        url = SiteSettings.load().absolute_url(reverse("accounts:set_password", args=[uid, token])) 
        if not url.startswith("http"):
            url = "http://localhost:8000" + url
        self.stdout.write(self.style.SUCCESS(f"Administrator {user.username} created."))
        self.stdout.write("Open this link to choose a password (valid for 3 days, works once):")
        self.stdout.write(url)

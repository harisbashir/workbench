from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in


class HeaderTests(TestCase):
    def test_security_headers(self):
        r = self.client.get(reverse("accounts:login"))
        self.assertIn("default-src 'self'", r["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", r["Content-Security-Policy"])
        self.assertEqual(r["X-Frame-Options"], "DENY")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")

    def test_signed_in_pages_are_not_cached(self):
        c = signed_in(make_user("eng"))
        self.assertEqual(c.get(reverse("core:dashboard"))["Cache-Control"], "no-store")


class SmokeTests(TestCase):
    def test_every_page_renders_for_every_role(self):
        """Smoke test: the main pages load without errors for each role."""
        for role in User.Role.values:
            c = signed_in(make_user(f"u-{role}", role=role))
            for url in ["/", "/my-work/", "/projects/", "/chat/", "/parts/", "/parts/boms/", "/parts/suppliers/",
                        "/production/", "/production/orders/", "/integrations/", "/help/", "/help/github/",
                        "/help/files/", "/help/time/", "/help/install/", "/files/", "/files/shared/", "/files/trash/",
                        "/time/", "/time/report/", "/notifications/", "/search/?q=pwr", "/accounts/profile/", "/accounts/security/"]:
                r = c.get(url, follow=True)
                self.assertEqual(r.status_code, 200, f"{role} {url}")

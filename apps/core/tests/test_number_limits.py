"""Number boxes refuse numbers too large to be real with a plain message, not the database's own limit or an error page."""
import importlib
import inspect
import pkgutil

from django import forms
from django.test import TestCase
from django.urls import reverse

import apps
from apps.accounts.models import User
from apps.core.forms import NumberLimits
from apps.core.testing import make_user, signed_in
from apps.firmware.tests.helpers import make_fixture
from apps.inventory.models import Part

HUGE = "99999999999999999999"


def all_forms():
    for info in pkgutil.walk_packages(apps.__path__, "apps."):
        if ".tests" in info.name or ".migrations" in info.name or ".management" in info.name:
            continue
        mod = importlib.import_module(info.name)
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, forms.BaseForm) and cls.__module__ == mod.__name__:
                yield cls


class NumberLimitTests(TestCase):
    def test_every_form_with_a_number_box_has_limits(self):
        missing = [f"{cls.__module__}.{cls.__name__}" for cls in all_forms()
                   if any(isinstance(f, forms.IntegerField) for f in cls.base_fields.values())
                   and not issubclass(cls, NumberLimits)]
        self.assertEqual(missing, [])

    def test_controller_order(self):
        f = make_fixture()
        r = signed_in(f.lead).post(reverse("firmware:controller_create", args=["PWR", f.board.pk]),
                                   {"designator": "U4", "name": "x", "order": HUGE})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Use a number up to 10,000,000.")
        self.assertNotContains(r, "9223372036854775807")
        self.assertContains(r, 'max="10000000"')

    def test_stock_adjust_is_refused_not_an_error_page(self):
        buyer = make_user("buyer", role=User.Role.PROCUREMENT)
        part = Part.objects.create(ipn="RES-0001", description="10k")
        r = signed_in(buyer).post(reverse("inventory:part_adjust", args=[part.pk]), {"mode": "add", "quantity": HUGE})
        self.assertEqual(r.status_code, 302)
        part.refresh_from_db()
        self.assertEqual(part.stock, 0)

    def test_own_limit_and_normal_numbers(self):
        from apps.core.forms import EmailSettingsForm
        form = EmailSettingsForm()
        self.assertEqual(form.fields["smtp_port"].max_value, 65535)
        from apps.inventory.forms import StockAdjustForm
        self.assertTrue(StockAdjustForm({"mode": "add", "quantity": "10000000"}).is_valid())
        self.assertFalse(StockAdjustForm({"mode": "add", "quantity": "10000001"}).is_valid())

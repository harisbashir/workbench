from django.test import TestCase
from django.urls import reverse

from apps.core.models import AuditLog
from apps.core.testing import signed_in
from apps.projects.models import Controller, default_board

from .helpers import make_fixture


class ControllerViewTests(TestCase):
    def setUp(self):
        self.f = make_fixture(slots=False)
        self.f.controller.delete()

    def create_url(self, key="PWR", board=None):
        return reverse("firmware:controller_create", args=[key, (board or self.f.board).pk])

    def post_create(self, client, firmwares, **extra):
        data = {"designator": "U1", "name": "Main MCU", "part": "STM32G474RE", "notes": "", "order": 0,
                "firmwares": [fw.pk for fw in firmwares], **extra}
        return client.post(self.create_url(), data)

    def test_member_adds_controller_with_firmware(self):
        r = self.post_create(signed_in(self.f.eng), [self.f.app, self.f.boot])
        self.assertEqual(r.status_code, 302)
        c = Controller.objects.get()
        self.assertEqual(c.slots.count(), 2)
        self.assertEqual(c.slots.get(firmware=self.f.boot).flash_order, 1)
        self.assertEqual(c.slots.get(firmware=self.f.app).flash_order, 2)
        self.assertTrue(AuditLog.objects.filter(action="controller.created").exists())

    def test_unticking_firmware_removes_slot(self):
        client = signed_in(self.f.eng)
        self.post_create(client, [self.f.app, self.f.boot])
        c = Controller.objects.get()
        url = reverse("firmware:controller_edit", args=["PWR", self.f.board.pk, c.pk])
        client.post(url, {"designator": "U1", "name": "Main MCU", "part": "", "notes": "", "order": 0, "firmwares": [self.f.app.pk]})
        self.assertEqual(list(c.slots.values_list("firmware", flat=True)), [self.f.app.pk])

    def test_outsider_cannot_edit(self):
        r = self.post_create(signed_in(self.f.outsider), [self.f.app])
        self.assertIn(r.status_code, (403, 404))
        self.assertFalse(Controller.objects.exists())

    def test_board_from_other_project_404(self):
        other_board = default_board(self.f.other)
        r = signed_in(self.f.eng).get(self.create_url(board=other_board))
        self.assertEqual(r.status_code, 404)

    def test_firmware_from_other_project_rejected(self):
        r = self.post_create(signed_in(self.f.eng), [self.f.other_fw])
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Controller.objects.exists())

    def test_delete_controller(self):
        client = signed_in(self.f.eng)
        self.post_create(client, [self.f.app])
        c = Controller.objects.get()
        client.post(reverse("firmware:controller_delete", args=["PWR", self.f.board.pk, c.pk]))
        self.assertFalse(Controller.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="controller.deleted").exists())

    def test_board_page_lists_controllers(self):
        self.post_create(signed_in(self.f.eng), [self.f.app])
        url = reverse("projects:hw_board", args=["PWR", self.f.board.pk])
        page = signed_in(self.f.eng).get(url).content.decode()
        self.assertIn("U1", page)
        self.assertIn("Main MCU", page)
        self.assertIn(self.create_url(), page)
        self.f.p.members.add(self.f.outsider)
        self.f.outsider.role = "viewer"
        self.f.outsider.save()
        page = signed_in(self.f.outsider).get(url).content.decode()
        self.assertIn("Main MCU", page)
        self.assertNotIn(self.create_url(), page)


class ControllerSafetyTests(TestCase):
    def setUp(self):
        from apps.firmware import sets
        self.f = make_fixture()
        sets.save_set(self.f.rev, {self.f.boot_slot.pk: self.f.boot100.pk, self.f.app_slot.pk: self.f.app100.pk}, self.f.eng)
        sets.set_lock(self.f.rev, True)
        self.edit_url = reverse("firmware:controller_edit", args=["PWR", self.f.board.pk, self.f.controller.pk])

    def test_locked_set_blocks_unticking_firmware(self):
        r = signed_in(self.f.eng).post(self.edit_url, {"designator": "U1", "name": "Main MCU", "part": "", "notes": "",
                                                       "order": 0, "firmwares": [self.f.app.pk]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.f.controller.slots.count(), 2)

    def test_locked_set_blocks_controller_delete(self):
        signed_in(self.f.eng).post(reverse("firmware:controller_delete", args=["PWR", self.f.board.pk, self.f.controller.pk]))
        self.assertTrue(Controller.objects.filter(pk=self.f.controller.pk).exists())

    def test_designator_must_be_simple(self):
        r = signed_in(self.f.eng).post(reverse("firmware:controller_create", args=["PWR", self.f.board.pk]),
                                       {"designator": "../../x", "name": "Bad", "part": "", "notes": "", "order": 0})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Controller.objects.filter(name="Bad").exists())

    def test_duplicate_insert_race_shows_form_error(self):
        from unittest import mock
        from django.db import IntegrityError
        url = reverse("firmware:controller_create", args=["PWR", self.f.board.pk])
        with mock.patch("apps.firmware.board_views.ControllerForm.save", side_effect=IntegrityError):
            r = signed_in(self.f.eng).post(url, {"designator": "U5", "name": "Race", "part": "", "notes": "", "order": 0})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already on this board")

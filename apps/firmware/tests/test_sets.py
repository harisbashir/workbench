from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from apps.firmware.models import ControllerSlot, FirmwareSetEntry
from apps.projects.models import Controller

from .helpers import make_fixture


class ModelTests(TestCase):
    def setUp(self):
        self.f = make_fixture()

    def test_controller_str_and_unique_designator(self):
        self.assertEqual(str(self.f.controller), "U1 Main MCU")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Controller.objects.create(board=self.f.board, designator="U1", name="Again")

    def test_slot_rejects_other_project_firmware(self):
        slot = ControllerSlot(controller=self.f.controller, firmware=self.f.other_fw)
        with self.assertRaises(ValidationError):
            slot.full_clean()

    def test_entry_unique_per_revision_slot(self):
        FirmwareSetEntry.objects.create(revision=self.f.rev, slot=self.f.app_slot, release=self.f.app100)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FirmwareSetEntry.objects.create(revision=self.f.rev, slot=self.f.app_slot, release=self.f.app110)

    def test_release_in_set_is_protected(self):
        FirmwareSetEntry.objects.create(revision=self.f.rev, slot=self.f.app_slot, release=self.f.app100)
        with self.assertRaises(ProtectedError):
            self.f.app100.delete()

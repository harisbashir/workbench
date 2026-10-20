from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from apps.firmware.models import ControllerSlot, FirmwareSetEntry
from apps.firmware import sets
from apps.projects.models import Board, Controller

from .helpers import make_fixture, release


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


class SetRuleTests(TestCase):
    def setUp(self):
        self.f = make_fixture()

    def choices(self, **kw):
        return {getattr(self.f, f"{k}_slot").pk: (v.pk if v else None) for k, v in kw.items()}

    def test_issues_recalled(self):
        self.assertIn(("bad", "Recalled — do not flash"), sets.issues_for(self.f.app090, self.f.rev))

    def test_issues_not_compatible_and_newer(self):
        lone = release(self.f.app, "0.8.0")
        self.assertIn(("warn", f"Not marked compatible with {self.f.rev.title}"), sets.issues_for(lone, self.f.rev))
        release(self.f.app, "1.2.0", revisions=[self.f.rev])
        self.assertIn(("warn", "Newer release 1.2.0 is available"), sets.issues_for(self.f.app100, self.f.rev))

    def test_set_rows_marks_empty_slot(self):
        rows = sets.set_rows(self.f.rev)
        self.assertEqual([r["slot"] for r in rows], [self.f.boot_slot, self.f.app_slot])
        for r in rows:
            self.assertIn(("warn", "No version chosen"), r["issues"])
        self.assertEqual(sets.missing_count(self.f.rev), 2)

    def test_save_set_saves_and_clears(self):
        self.assertEqual(sets.save_set(self.f.rev, self.choices(boot=self.f.boot100, app=self.f.app100), self.f.eng), [])
        self.assertEqual(FirmwareSetEntry.objects.filter(revision=self.f.rev).count(), 2)
        self.assertEqual(sets.save_set(self.f.rev, self.choices(app=None), self.f.eng), [])
        self.assertEqual(list(FirmwareSetEntry.objects.values_list("release", flat=True)), [self.f.boot100.pk])

    def test_released_revision_only_accepts_released(self):
        self.f.rev.status = "released"
        self.f.rev.save()
        errors = sets.save_set(self.f.rev, self.choices(app=self.f.app110), self.f.eng)
        self.assertEqual(len(errors), 1)
        self.assertFalse(FirmwareSetEntry.objects.exists())

    def test_draft_never_selectable(self):
        draft = release(self.f.app, "2.0.0", status="draft", revisions=[self.f.rev])
        self.assertEqual(len(sets.save_set(self.f.rev, self.choices(app=draft), self.f.eng)), 1)

    def test_save_set_refuses_other_project_release(self):
        errors = sets.save_set(self.f.rev, {self.f.app_slot.pk: self.f.other_rel.pk}, self.f.eng)
        self.assertEqual(len(errors), 1)
        self.assertFalse(FirmwareSetEntry.objects.exists())

    def test_unknown_slot_ignored(self):
        other_board = Board.objects.create(project=self.f.p, name="Other board")
        c = Controller.objects.create(board=other_board, designator="U9", name="Elsewhere")
        slot = ControllerSlot.objects.create(controller=c, firmware=self.f.app)
        self.assertEqual(sets.save_set(self.f.rev, {slot.pk: self.f.app100.pk}, self.f.eng), [])
        self.assertFalse(FirmwareSetEntry.objects.exists())

    def test_locked_set_refuses_changes(self):
        self.assertTrue(sets.set_lock(self.f.rev, True))
        self.assertFalse(sets.set_lock(self.f.rev, True))
        errors = sets.save_set(self.f.rev, self.choices(app=self.f.app100), self.f.eng)
        self.assertEqual(errors, ["This firmware set is locked. Ask a lead to unlock it."])
        self.assertFalse(FirmwareSetEntry.objects.exists())

    def test_save_set_twice_is_idempotent(self):
        c = self.choices(boot=self.f.boot100, app=self.f.app100)
        sets.save_set(self.f.rev, c, self.f.eng)
        sets.save_set(self.f.rev, c, self.f.eng)
        self.assertEqual(FirmwareSetEntry.objects.count(), 2)

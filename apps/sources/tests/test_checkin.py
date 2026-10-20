from django.test import TestCase

from apps.sources import checkin
from apps.sources.models import Lock

from .helpers import commit, files, make_repo


class CheckinTests(TestCase):
    def setUp(self):
        self.f = make_repo()
        self.repo = self.f.repo

    def tree(self):
        return {p: b.read().decode() for p, b in checkin.tree(self.repo.latest()).items()}

    def test_first_checkin_creates_v1(self):
        v = commit(self.repo, self.f.eng, message="First", **{"src/main.c": "int x;\n", "README.md": "# Hi\n"})
        self.assertEqual((v.number, v.added, v.message, v.author), (1, 2, "First", self.f.eng))
        self.repo.refresh_from_db()
        self.assertEqual(self.repo.latest_number, 1)
        self.assertEqual(self.tree(), {"README.md": "# Hi\n", "src/main.c": "int x;\n"})

    def test_update_mode_keeps_missing_files(self):
        commit(self.repo, self.f.eng, **{"a.c": "1", "b.c": "2"})
        v = commit(self.repo, self.f.eng, **{"a.c": "1\n2\n"})
        self.assertEqual((v.changed, v.deleted), (1, 0))
        self.assertEqual(set(self.tree()), {"a.c", "b.c"})

    def test_replace_mode_deletes_missing_files(self):
        commit(self.repo, self.f.eng, **{"a.c": "1", "b.c": "2"})
        v = commit(self.repo, self.f.eng, mode="replace", **{"a.c": "1"})
        self.assertEqual(v.deleted, 1)
        self.assertEqual(set(self.tree()), {"a.c"})

    def test_line_counts(self):
        commit(self.repo, self.f.eng, **{"a.c": "1\n2\n3\n"})
        v = commit(self.repo, self.f.eng, **{"a.c": "1\nTWO\n3\n4\n"})
        self.assertEqual((v.lines_added, v.lines_removed), (2, 1))

    def test_unchanged_checkin_refused(self):
        commit(self.repo, self.f.eng, **{"a.c": "1"})
        version, plan = checkin.commit(self.repo, files(**{"a.c": "1"}), "update", 1, "Again", self.f.eng)
        self.assertIsNone(version)
        self.assertFalse(plan.has_changes)

    def test_stale_file_refused(self):
        commit(self.repo, self.f.eng, **{"main.c": "1", "other.c": "1"})
        commit(self.repo, self.f.eng, base=1, **{"main.c": "A"})
        staged = files(**{"main.c": "B", "other.c": "B"})
        version, plan = checkin.commit(self.repo, staged, "update", 1, "B's work", self.f.eng2)
        self.assertIsNone(version)
        self.assertEqual([p for p, _ in plan.refused], ["main.c"])
        self.assertIn("v2", plan.refused[0][1])
        version, plan = checkin.commit(self.repo, staged, "update", 1, "B's work", self.f.eng2, allow_partial=True)
        self.assertEqual(version.number, 3)
        self.assertEqual(self.tree(), {"main.c": "A", "other.c": "B"})

    def test_stale_delete_refused_in_replace_mode(self):
        commit(self.repo, self.f.eng, **{"main.c": "1", "keep.c": "1"})
        commit(self.repo, self.f.eng, base=1, **{"main.c": "A"})
        plan = checkin.plan(self.repo, files(**{"keep.c": "1"}), "replace", 1, self.f.eng2)
        self.assertEqual([p for p, _ in plan.refused], ["main.c"])

    def test_kicad_needs_lock(self):
        commit(self.repo, self.f.eng, **{"board.kicad_pcb": "(kicad_pcb)"})
        plan = checkin.plan(self.repo, files(**{"board.kicad_pcb": "(kicad_pcb 2)"}), "update", 1, self.f.eng)
        self.assertIn("Check out", plan.refused[0][1])
        self.assertIsNone(checkin.lock(self.repo, "board.kicad_pcb", self.f.eng))
        plan = checkin.plan(self.repo, files(**{"board.kicad_pcb": "(kicad_pcb 2)"}), "update", 1, self.f.eng)
        self.assertEqual((plan.refused, plan.changed), ([], ["board.kicad_pcb"]))

    def test_new_kicad_file_needs_no_lock(self):
        plan = checkin.plan(self.repo, files(**{"board.kicad_sch": "(kicad_sch)"}), "update", 0, self.f.eng)
        self.assertEqual(plan.refused, [])

    def test_lock_by_other_blocks(self):
        commit(self.repo, self.f.eng, **{"a.c": "1", "board.kicad_sch": "(x)"})
        self.assertIsNone(checkin.lock(self.repo, "a.c", self.f.eng))
        self.assertIn("eng", checkin.lock(self.repo, "a.c", self.f.eng2))
        plan = checkin.plan(self.repo, files(**{"a.c": "2"}), "update", 1, self.f.eng2)
        self.assertIn("Checked out by", plan.refused[0][1])
        self.assertIsNotNone(checkin.lock(self.repo, "missing.c", self.f.eng))
        self.assertFalse(checkin.unlock(self.repo, "a.c", self.f.eng2))
        self.assertTrue(checkin.unlock(self.repo, "a.c", self.f.eng2, force=True))

    def test_locks_released_after_checkin(self):
        commit(self.repo, self.f.eng, **{"board.kicad_sch": "(x)"})
        checkin.lock(self.repo, "board.kicad_sch", self.f.eng)
        commit(self.repo, self.f.eng, **{"board.kicad_sch": "(y)"})
        self.assertFalse(Lock.objects.exists())

    def test_numbers_sequential_under_lock(self):
        commit(self.repo, self.f.eng, **{"a.c": "1", "b.c": "1"})
        v2 = commit(self.repo, self.f.eng, base=1, **{"a.c": "2"})
        v3 = commit(self.repo, self.f.eng2, base=1, **{"b.c": "2"})
        self.assertEqual((v2.number, v3.number, v3.parent), (2, 3, v2))

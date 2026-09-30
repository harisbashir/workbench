from django.test import SimpleTestCase, TestCase

from apps.firmware.models import Firmware, FirmwareRelease, sort_key
from apps.projects.models import Project


class PrereleaseOrderTests(SimpleTestCase):
    def test_numeric_identifiers_compare_numerically(self):
        versions = ["1.0.0", "1.0.0-rc.10", "1.0.0-alpha", "1.0.0-rc.2", "1.0.0-beta.11", "1.0.0-alpha.1",
                    "1.0.0-beta.2", "1.0.0-beta", "0.9.9", "1.0.0-rc.1"]
        expected = ["0.9.9", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                    "1.0.0-rc.1", "1.0.0-rc.2", "1.0.0-rc.10", "1.0.0"]
        self.assertEqual(sorted(versions, key=sort_key), expected)

    def test_numeric_below_alphanumeric(self):
        self.assertLess(sort_key("1.0.0-1"), sort_key("1.0.0-a"))


class ReleaseSortingTests(TestCase):
    def test_releases_sorted_and_recommended(self):
        p = Project.objects.create(key="PWR", name="Power")
        fw = Firmware.objects.create(project=p, name="Main")
        for v in ["1.0.0-rc.2", "1.0.0-rc.10", "1.0.0-rc.9"]:
            FirmwareRelease.objects.create(firmware=fw, version=v, status="released")
        self.assertEqual([r.version for r in fw.releases_sorted()], ["1.0.0-rc.10", "1.0.0-rc.9", "1.0.0-rc.2"])
        self.assertEqual(fw.latest_released.version, "1.0.0-rc.10")
        self.assertEqual(fw.suggest_next_version(), "1.0.0")

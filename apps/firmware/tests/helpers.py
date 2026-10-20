"""A small project with a board, one controller and two firmwares, shared by the firmware-set tests."""
from types import SimpleNamespace

from apps.accounts.models import User
from apps.core.testing import make_user
from apps.firmware.models import Firmware, FirmwareRelease
from apps.projects.models import Project, Revision, default_board


def release(fw, version, status="released", revisions=()):
    r = FirmwareRelease.objects.create(firmware=fw, version=version, status=status)
    r.revisions.set(revisions)
    return r


def make_fixture(slots=True):
    from apps.firmware.models import ControllerSlot
    from apps.projects.models import Controller

    lead = make_user("lead", role=User.Role.LEAD)
    eng = make_user("eng")
    outsider = make_user("out")
    p = Project.objects.create(key="PWR", name="Power", lead=lead)
    p.members.add(eng)
    board = default_board(p)
    rev = Revision.objects.create(project=p, board=board, name="Rev A")
    app = Firmware.objects.create(project=p, name="Main application")
    boot = Firmware.objects.create(project=p, name="Bootloader")
    f = SimpleNamespace(lead=lead, eng=eng, outsider=outsider, p=p, board=board, rev=rev, app=app, boot=boot)
    f.app100 = release(app, "1.0.0", revisions=[rev])
    f.app110 = release(app, "1.1.0", status="testing", revisions=[rev])
    f.app090 = release(app, "0.9.0", status="recalled", revisions=[rev])
    f.boot100 = release(boot, "1.0.0", revisions=[rev])
    f.controller = Controller.objects.create(board=board, designator="U1", name="Main MCU", part="STM32G474RE")
    if slots:
        f.boot_slot = ControllerSlot.objects.create(controller=f.controller, firmware=boot, flash_order=1)
        f.app_slot = ControllerSlot.objects.create(controller=f.controller, firmware=app, flash_order=2)
    other = Project.objects.create(key="OTH", name="Other", lead=lead)
    f.other = other
    f.other_fw = Firmware.objects.create(project=other, name="Other app")
    f.other_rel = release(f.other_fw, "1.0.0")
    return f

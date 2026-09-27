import csv
import io
import zipfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_user, signed_in
from apps.design import demo_board
from apps.design.models import DesignFile
from apps.inventory.models import BomLine, Part, Supplier, expand_refs
from apps.production.fab import parse_placements
from apps.production.models import BuildOrder
from apps.projects.models import Project, Revision


class PlacementParsingTests(SimpleTestCase):
    def test_kicad_csv(self):
        rows, _ = parse_placements(b'Ref,Val,Package,PosX,PosY,Rot,Side\n"C1","100nF","C_0603",10.5,-3.25,90.0,top\n"J2","Conn","JST",40,5,0,bottom\n')
        self.assertEqual(rows[0], {"ref": "C1", "val": "100nF", "package": "C_0603", "x": 10.5, "y": -3.25, "rot": 90.0, "side": "top"})
        self.assertEqual(rows[1]["side"], "bottom")

    def test_kicad_ascii_pos(self):
        text = ("### Footprint positions - created on 2026-09-22 ###\n## Unit = mm, Angle = deg.\n## Side : All\n"
                "# Ref     Val       Package        PosX       PosY       Rot  Side\n"
                "C1        100nF     C_0603      12.0000   -4.5000   180.0000  top\n## End\n")
        rows, _ = parse_placements(text.encode())
        self.assertEqual((rows[0]["ref"], rows[0]["x"], rows[0]["y"], rows[0]["rot"]), ("C1", 12.0, -4.5, 180.0))

    def test_jlc_cpl_and_altium_with_preamble(self):
        rows, _ = parse_placements(b"Designator,Mid X,Mid Y,Layer,Rotation\nR1,10.2mm,5mm,T,270\n")
        self.assertEqual((rows[0]["x"], rows[0]["side"], rows[0]["rot"]), (10.2, "top", 270.0))
        altium = ("Altium Designer Pick and Place Locations\nUnits used in this file: mil\n\n"
                  '"Designator","Comment","Layer","Footprint","Center-X(mil)","Center-Y(mil)","Rotation"\n'
                  '"U1","MCU","BottomLayer","QFN","1000mil","500mil","90"\n')
        rows, _ = parse_placements(altium.encode())
        self.assertAlmostEqual(rows[0]["x"], 25.4)
        self.assertEqual(rows[0]["side"], "bottom")

    def test_not_a_placement_file(self):
        rows, warnings = parse_placements(b"a,b,c\n1,2,3\n")
        self.assertEqual(rows, [])
        self.assertTrue(warnings)

    def test_expand_refs(self):
        self.assertEqual(expand_refs("R1, R2;R5-R7 C3-5"), ["R1", "R2", "R5", "R6", "R7", "C3", "C4", "C5"])


class FabFlowTests(TestCase):
    def setUp(self):
        self.buyer = make_user("buyer", role=User.Role.PROCUREMENT)
        self.eng = make_user("eng")
        self.outsider = make_user("out")
        self.lcsc = Supplier.objects.create(name="LCSC")
        self.jlc = Supplier.objects.create(name="JLCPCB", kind="assembly")
        self.p = Project.objects.create(key="PWR", name="Power")
        self.p.members.add(self.eng)
        self.rev = Revision.objects.create(project=self.p, name="Rev B")

        def part(desc, value, fp, stock, sku=""):
            return Part.objects.create(description=desc, value=value, footprint=fp, stock=stock, supplier=self.lcsc,
                                       supplier_sku=sku, mpn=f"MPN-{value}", unit_cost=Decimal("0.01"))
        self.cap = part("100nF 0603", "100nF", "Capacitor_SMD:C_0603_1608Metric", 1000, "C14663")
        self.conn = part("JST PH 4 pin", "Conn_01x04", "Connector_JST:JST_PH_B4B-PH-K_1x04_P2.00mm_Vertical", 40)
        self.esd = part("ESD", "USBLC6-2SC6", "Package_TO_SOT_SMD:SOT-23-6", 10)
        self.r0 = part("0R", "0R", "Resistor_SMD:R_0603_1608Metric", 100)
        self.pcb = Part.objects.create(description="Bare PCB", category="pcb", stock=0)
        BomLine.objects.create(revision=self.rev, part=self.cap, quantity=5, references="C1, C2, C5, C6, C9")
        self.l_conn = BomLine.objects.create(revision=self.rev, part=self.conn, quantity=1, references="J2")
        self.l_esd = BomLine.objects.create(revision=self.rev, part=self.esd, quantity=1, references="U3", fitted_by="house")
        BomLine.objects.create(revision=self.rev, part=self.r0, quantity=1, references="R7", dnp=True)
        BomLine.objects.create(revision=self.rev, part=self.pcb, quantity=1, references="PCB1")
        DesignFile.store(self.rev, SimpleUploadedFile("g.zip", demo_board.gerber_zip()), self.eng)
        DesignFile.store(self.rev, SimpleUploadedFile("pwr-board-pos.csv", demo_board.pos_csv()), self.eng)

    def test_suggest_and_toggle_fitted_by(self):
        c = signed_in(self.eng)
        c.post(reverse("production:bom_fitted", args=[self.rev.pk]), {"action": "suggest"})
        self.l_conn.refresh_from_db()
        self.assertEqual(self.l_conn.fitted_by, "house", "through-hole connector suggested for hand fitting")
        r = c.post(reverse("production:bom_fitted", args=[self.rev.pk]), {"line": self.l_esd.pk, "to": "fab"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.json()["fitted_by"], "fab")
        self.assertEqual(signed_in(self.outsider).post(reverse("production:bom_fitted", args=[self.rev.pk]),
                                                       {"line": self.l_esd.pk, "to": "house"}).status_code, 403)
        self.assertContains(c.get(reverse("inventory:bom", args=[self.rev.pk])), "In house")

    def test_order_files_leave_out_in_house_and_dnp(self):
        c = signed_in(self.eng)
        url = reverse("production:fab_package", args=[self.rev.pk])
        page = c.get(url)
        self.assertContains(page, "Fitted in house")
        self.assertContains(page, "<td class=\"small\">U3</td>", html=False)
        bom = list(csv.reader(io.StringIO(c.get(url + "?for=jlc&get=bom").content.decode("utf-8-sig"))))
        self.assertEqual(bom[0], ["Comment", "Designator", "Footprint", "LCSC Part #"])
        designators = ",".join(r[1] for r in bom[1:])
        self.assertIn("C1,C2,C5,C6,C9", designators)
        for left_out in ("U3", "R7", "PCB1"):
            self.assertNotIn(left_out, designators)
        self.assertEqual(bom[1][3], "C14663")
        cpl = list(csv.reader(io.StringIO(c.get(url + "?for=jlc&get=cpl").content.decode("utf-8-sig"))))
        refs = [r[0] for r in cpl[1:]]
        self.assertEqual(cpl[0], ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        self.assertIn("J2", refs)
        self.assertNotIn("U3", refs)
        self.assertNotIn("R7", refs)
        seeed = c.get(url + "?for=seeed&get=bom").content.decode("utf-8-sig")
        self.assertIn("Manufacturer Part Number or Seeed SKU", seeed)
        z = zipfile.ZipFile(io.BytesIO(c.get(url + "?for=jlc&get=zip").content))
        names = z.namelist()
        self.assertIn("PWR-RevB-gerbers.zip", names)
        self.assertIn("PWR-RevB-CPL-JLCPCB.csv", names)
        self.assertTrue(any(n.startswith("not for JLCPCB") for n in names))

    def test_fab_build_flow(self):
        buyer = signed_in(self.buyer)
        b = BuildOrder.objects.create(revision=self.rev, quantity=4, manufacturer=self.jlc)
        self.assertEqual([r["part"] for r in b.requirements()], [self.esd], "only in-house parts come from our stock")
        stage = reverse("production:build_stage", args=[b.pk])
        buyer.post(stage, {"action": "fab_ordered", "fab_order_ref": "SO123"})
        b.refresh_from_db()
        self.esd.refresh_from_db()
        self.cap.refresh_from_db()
        self.assertEqual((b.stage, b.status, b.fab_order_ref), ("at_fab", "in_progress", "SO123"))
        self.assertEqual((self.esd.stock, self.cap.stock), (6, 1000))
        self.assertEqual([s.title for s in b.steps.all()], ["Fit U3"])
        buyer.post(stage, {"action": "received", "received_qty": 3})
        b.refresh_from_db()
        self.assertEqual((b.stage, b.received_qty), ("finishing", 3))
        # Anyone on the project can record progress at the bench
        eng = signed_in(self.eng)
        eng.post(stage, {"action": "add_step", "title": "Wash flux"})
        step = b.steps.get(title="Fit U3")
        url = reverse("production:build_step", args=[b.pk, step.pk])
        r = eng.post(url, {"how": "5"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.json()["done"], 3, "can't count more boards than were received")
        self.assertFalse(r.json()["all_done"])
        eng.post(url, {"how": "-1"})
        step.refresh_from_db()
        self.assertEqual(step.done_qty, 2)
        wash = b.steps.get(title="Wash flux")
        eng.post(reverse("production:build_step", args=[b.pk, wash.pk]), {"how": "all"})
        r = eng.post(url, {"how": "all"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertTrue(r.json()["all_done"])
        self.assertEqual(signed_in(self.outsider).post(url, {"how": "1"}).status_code, 403)
        page = eng.get(b.get_absolute_url())
        self.assertContains(page, "Finishing in house")
        self.assertContains(page, "board-markers")   # parts marked on the board picture
        self.assertEqual(eng.get(reverse("production:traveler", args=[b.pk])).status_code, 200)
        eng.post(stage, {"action": "finished"})
        b.refresh_from_db()
        self.assertEqual(b.stage, "testing")
        buyer.post(reverse("production:build_action", args=[b.pk]), {"action": "complete", "passed": 3, "failed": 0})
        b.refresh_from_db()
        self.assertEqual((b.stage, b.status, b.completed_qty), ("done", "completed", 3))

    def test_in_house_build_uses_all_parts(self):
        b = BuildOrder.objects.create(revision=self.rev, quantity=2, assembly="house")
        signed_in(self.buyer).post(reverse("production:build_stage", args=[b.pk]), {"action": "start_house", "force": "1"})
        b.refresh_from_db()
        self.cap.refresh_from_db()
        self.assertEqual((b.stage, self.cap.stock), ("finishing", 990))
        self.assertEqual(b.steps.count(), 3, "every fitted part except the bare PCB becomes a step")

    def test_engineers_cannot_order(self):
        b = BuildOrder.objects.create(revision=self.rev, quantity=2)
        r = signed_in(self.eng).post(reverse("production:build_stage", args=[b.pk]), {"action": "fab_ordered"})
        self.assertEqual(r.status_code, 403)

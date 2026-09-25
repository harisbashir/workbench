"""Fill an empty database with realistic example data so people can explore.

    python manage.py seed_demo              # password for every demo user: see --password
    python manage.py seed_demo --password "a-strong-demo-password"

Never run this on your production database.
"""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.chat.models import Channel, Message
from apps.integrations import github
from apps.inventory import bom_import
from apps.inventory.models import BomLine, Part, StockMovement, Supplier
from apps.production.models import BuildOrder, PurchaseOrder
from apps.projects.models import Project, Revision, Task, TaskComment, log_activity

PEOPLE = [
    # username, first, last, role, title, tz, github
    ("haris", "Haris", "Bashir", User.Role.ADMIN, "Engineering Manager", "America/Toronto", "haris-b"),
    ("ayesha", "Ayesha", "Khan", User.Role.LEAD, "Hardware Lead", "Asia/Karachi", "ayesha-k"),
    ("bilal", "Bilal", "Ahmed", User.Role.ENGINEER, "Firmware Engineer", "Asia/Karachi", "bilal-fw"),
    ("sana", "Sana", "Malik", User.Role.ENGINEER, "PCB Design Engineer", "Asia/Karachi", "sana-pcb"),
    ("usman", "Usman", "Raza", User.Role.ENGINEER, "Test Engineer", "Asia/Karachi", "usman-r"),
    ("fatima", "Fatima", "Noor", User.Role.PROCUREMENT, "Procurement & Production", "Asia/Karachi", ""),
]


class Command(BaseCommand):
    help = "Load example projects, tasks, parts, orders and chat for a demo. Only for empty databases."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="Workbench-demo-2026!", help="Password for all demo users.")
        parser.add_argument("--force", action="store_true", help="Run even if data already exists.")

    @transaction.atomic
    def handle(self, *args, **o):
        if Project.objects.exists() and not o["force"]:
            raise CommandError("The database already has projects. Use --force if you really want to add demo data.")
        now = timezone.now()
        u = {}
        for username, first, last, role, title, tz, gh in PEOPLE:
            user, _ = User.objects.get_or_create(username=username, defaults=dict(
                first_name=first, last_name=last, role=role, job_title=title, time_zone=tz, github_username=gh,
                email=f"{username}@example.com", is_superuser=(role == User.Role.ADMIN), is_staff=(role == User.Role.ADMIN)))
            user.set_password(o["password"])
            user.save()
            u[username] = user

        # Suppliers
        s = {}
        for name, kind, country, lead, web in [
            ("DigiKey", "distributor", "United States", 7, "https://www.digikey.com"),
            ("LCSC", "distributor", "China", 10, "https://www.lcsc.com"),
            ("JLCPCB", "pcb", "China", 12, "https://jlcpcb.com"),
            ("Lahore Electronics Assembly", "assembly", "Pakistan", 14, ""),
        ]:
            s[name] = Supplier.objects.get_or_create(name=name, defaults=dict(kind=kind, country=country, lead_time_days=lead, website=web))[0]

        # Projects
        pwr = Project.objects.create(key="PWR", name="Power management board", lead=u["ayesha"], github_repo="acme-embedded/power-board",
                                     description="USB-C powered buck converter board with an STM32G0 supervisor for the field logger product.")
        pwr.members.add(u["ayesha"], u["bilal"], u["sana"], u["usman"], u["haris"])
        sens = Project.objects.create(key="SENS", name="Environmental sensor node", lead=u["ayesha"], github_repo="acme-embedded/sensor-node",
                                      description="Low-power LoRa sensor node: temperature, humidity and pressure.")
        sens.members.add(u["ayesha"], u["bilal"], u["sana"], u["haris"])
        for p in (pwr, sens):
            Channel.objects.create(name=p.key.lower(), slug=p.key.lower(), kind=Channel.Kind.PROJECT, project=p,
                                   topic=f"Discussion for {p.name}. GitHub and task updates are posted here automatically.")
        general = Channel.objects.create(name="general", slug="general", kind=Channel.Kind.PUBLIC, topic="Company-wide announcements and questions")
        general.members.add(*u.values())
        fw = Channel.objects.create(name="firmware", slug="firmware", kind=Channel.Kind.PUBLIC, topic="Firmware practices, toolchains, debugging help")
        fw.members.add(u["bilal"], u["haris"], u["ayesha"])

        rev_a = Revision.objects.create(project=pwr, name="Rev A", status=Revision.Status.RELEASED, git_ref="v1.0-revA", notes="First prototype run of 10.")
        rev_b = Revision.objects.create(project=pwr, name="Rev B", status=Revision.Status.DESIGN, target_date=(now + timedelta(days=21)).date(),
                                        notes="Adds USB ESD protection and a load switch; fixes Rev A feedback divider.")
        sens_a = Revision.objects.create(project=sens, name="EVT", status=Revision.Status.DESIGN, target_date=(now + timedelta(days=45)).date())
        for rev in (rev_a, rev_b, sens_a):
            rev.add_default_checks()
        rev_a.checks.update(done_by=u["ayesha"], done_at=now - timedelta(days=60))
        for c in rev_b.checks.all()[:4]:
            c.done_by, c.done_at = (u["sana"] if c.order % 2 else u["haris"]), now - timedelta(days=1, hours=c.order)
            c.save()

        # BOM for Rev B from the sample KiCad CSV, using the real importer.
        sample = Path(settings.BASE_DIR) / "docs" / "examples" / "sample_bom_rev_b.csv"
        rows, _ = bom_import.parse(sample.read_bytes())
        prices = {"100nF": "0.0021", "22uF": "0.0480", "10uF": "0.0120", "4.7uF": "0.0090", "10k": "0.0010", "100k": "0.0010",
                  "31.6k": "0.0012", "5.1k": "0.0010", "0R": "0.0008", "4.7uH": "0.3100", "TPS62133": "1.9500",
                  "STM32G031K8": "1.4200", "USBLC6-2SC6": "0.1100", "LED_Green": "0.0300", "USB_C_Receptacle": "0.5200",
                  "Conn_01x04": "0.0900", "AO3401A": "0.0400"}
        stock = {"100nF": 2400, "22uF": 180, "10uF": 300, "4.7uF": 40, "10k": 3800, "100k": 900, "31.6k": 0, "5.1k": 950, "0R": 500,
                 "4.7uH": 35, "TPS62133": 12, "STM32G031K8": 60, "USBLC6-2SC6": 0, "LED_Green": 400, "USB_C_Receptacle": 22,
                 "Conn_01x04": 150, "AO3401A": 0}
        for r in rows:
            part = Part.objects.create(description=r["description"], category=r["category"], value=r["value"], footprint=r["footprint"],
                                       manufacturer=r["manufacturer"], mpn=r["mpn"], datasheet_url=r["datasheet"],
                                       supplier=s["LCSC"] if r["category"] in ("resistor", "capacitor", "diode", "transistor") else s["DigiKey"],
                                       unit_cost=Decimal(prices.get(r["value"], "0.05")), min_stock=50 if r["category"] in ("resistor", "capacitor") else 10,
                                       location="Lahore lab · " + ("Reel rack" if r["category"] in ("resistor", "capacitor") else "Cabinet B"))
            if stock.get(r["value"]):
                part.adjust_stock(stock[r["value"]], StockMovement.Reason.ADJUST, user=u["fatima"], reference="Opening stock count")
            BomLine.objects.create(revision=rev_b, part=part, quantity=r["quantity"], references=r["references"], dnp=r["dnp"])
        pcb_b = Part.objects.create(ipn="PCB-0002", description="PWR bare PCB Rev B, 2-layer 1.6mm ENIG", category="pcb", supplier=s["JLCPCB"], unit_cost=Decimal("1.20"))
        BomLine.objects.create(revision=rev_b, part=pcb_b, quantity=1, references="PCB1")
        # Rev A BOM: same minus ESD/load switch, older divider
        for line in rev_b.bom_lines.select_related("part"):
            if line.part.value in ("USBLC6-2SC6", "AO3401A", "31.6k"):
                continue
            if line.part.value == "100nF":  # Rev B added one decoupling cap (C9)
                BomLine.objects.create(revision=rev_a, part=line.part, quantity=4, references="C1, C2, C5, C6")
                continue
            BomLine.objects.create(revision=rev_a, part=line.part, quantity=line.quantity, references=line.references, dnp=line.dnp)
        r33 = Part.objects.create(description="33k 1% 0603 resistor", category="resistor", value="33k", footprint="Resistor_SMD:R_0603_1608Metric",
                                  manufacturer="Yageo", mpn="RC0603FR-0733KL", supplier=s["LCSC"], unit_cost=Decimal("0.0010"), lifecycle=Part.Lifecycle.ACTIVE)
        BomLine.objects.create(revision=rev_a, part=r33, quantity=1, references="R4")

        # Tasks
        def task(project, title, kind, status, assignee, reviewer=None, prio=2, days=None, rev=None, desc=""):
            t = Task.objects.create(project=project, title=title, kind=kind, status=status, assignee=assignee, reviewer=reviewer,
                                    priority=prio, due_date=(now + timedelta(days=days)).date() if days is not None else None,
                                    revision=rev, description=desc, created_by=u["ayesha"],
                                    completed_at=now - timedelta(days=2) if status == Task.Status.DONE else None)
            log_activity(project, f"created {t.key} “{t.title}”", actor=u["ayesha"], task=t, url=t.get_absolute_url())
            return t

        T = Task.Status
        K = Task.Kind
        t1 = task(pwr, "Add USB-C ESD protection (USBLC6-2SC6) near J1", K.SCHEMATIC, T.IN_PROGRESS, u["sana"], u["haris"], 3, 4, rev_b,
                  "Rev A failed the 8 kV contact ESD test on the USB-C connector.\n\n**Done when:** USBLC6-2SC6 on D+/D-/VBUS, placed within 5 mm of J1, ERC clean.")
        t2 = task(pwr, "Fix feedback divider for 3.3 V output (R4 → 31.6k)", K.SCHEMATIC, T.DONE, u["sana"], u["ayesha"], 3, None, rev_b,
                  "Rev A output measured 3.52 V. Recalculate divider for TPS62133 FB = 0.8 V.")
        task(pwr, "Route Rev B layout and run DRC", K.PCB, T.TODO, u["sana"], u["haris"], 3, 10, rev_b,
                  "Keep the buck converter hot loop tight. See TI layout guidelines in the datasheet section 10.")
        t4 = task(pwr, "Brown-out detection and safe shutdown in firmware", K.FIRMWARE, T.IN_PROGRESS, u["bilal"], u["haris"], 2, 6, rev_b,
                  "Use the STM32G0 PVD at 2.9 V. Flush logs to flash before shutdown. Target < 5 ms from PVD interrupt to flash write complete.")
        task(pwr, "I2C driver for the fuel gauge with DMA", K.FIRMWARE, T.TODO, u["bilal"], u["haris"], 2, 12, rev_b)
        task(pwr, "Rev A thermal test at 2 A continuous load", K.TEST, T.DONE, u["usman"], u["ayesha"], 2, None, rev_a,
                  "Result: U1 case 71 °C at 25 °C ambient after 30 min. Within limits.")
        task(pwr, "Write production test procedure for Rev B", K.TEST, T.TODO, u["usman"], u["ayesha"], 2, 18, rev_b)
        task(pwr, "Watchdog configuration review", K.FIRMWARE, T.TODO, u["bilal"], u["haris"], 4, -1, rev_b,
                  "IWDG is currently disabled in release builds. Enable with a 500 ms window.")
        task(sens, "Select LoRa module (RFM95W vs. SX1262-based)", K.SCHEMATIC, T.IN_PROGRESS, u["sana"], u["ayesha"], 2, 9, sens_a)
        task(sens, "Low-power sleep mode, target < 5 µA", K.FIRMWARE, T.TODO, u["bilal"], u["haris"], 3, 20, sens_a)
        task(sens, "BME280 driver and calibration", K.FIRMWARE, T.TODO, None, u["haris"], 2, 25, sens_a)

        TaskComment.objects.create(task=t1, author=u["sana"], body="Placed U3 right behind J1. VBUS line goes through the TVS first. Will push the schematic tonight.")
        TaskComment.objects.create(task=t1, author=u["haris"], body="Looks right. Please also check the ESD diode's capacitance won't hurt USB 2.0 full-speed — datasheet says 3.5 pF typ, should be fine.")
        TaskComment.objects.create(task=t4, author=u["bilal"], body="PVD interrupt fires reliably at 2.92 V on the bench. Flash write takes 3.1 ms worst case. @haris can you review the approach before I clean it up?")
        TaskComment.objects.create(task=t2, author=u["ayesha"], body="Verified on the Rev A board with a rework: 3.31 V at no load, 3.29 V at 2 A. Approved.")

        # Chat
        ch = Channel.objects.get(project=pwr)
        chat = [
            (u["ayesha"], "Morning all. Rev B target is three weeks out — PWR-1 and PWR-3 are the critical path."),
            (u["sana"], "ESD schematic is nearly done. I'll open the PR today so @haris can review in his morning."),
            (u["bilal"], "PR #44 for brown-out is up. CI is green. Main question: is 3 ms flush time acceptable?"),
            (u["haris"], "3 ms is fine — the bulk cap gives us ~8 ms at 2.9 V. I'll review PWR-4 today. Please add the scope capture to the task."),
            (u["fatima"], "FYI: USBLC6-2SC6 and AO3401A are out of stock. I'll order from LCSC once the Rev B BOM is final."),
        ]
        for i, (who, text) in enumerate(chat):
            m = Message.objects.create(channel=ch, author=who, body=text)
            Message.objects.filter(pk=m.pk).update(created_at=now - timedelta(hours=5 - i))
        Message.objects.create(channel=general, author=u["haris"], body="Welcome to Workbench! Start with **Help & guides** in the menu. Questions about the tool go here.")
        Message.objects.create(channel=fw, author=u["bilal"], body="Reminder: we use `arm-none-eabi-gcc 13.2` and CMake presets. Run `cmake --preset release` before opening a PR.")
        dm = Channel.direct_between(u["haris"], u["ayesha"])
        Message.objects.create(channel=dm, author=u["ayesha"], body="Can we move the Rev B design review to Thursday 6 pm PKT / 9 am Toronto?")

        # GitHub activity, processed by the real webhook handler.
        def pr_payload(action, number, title, branch, user, merged=False, draft=False, state="open", body=""):
            return {"action": action, "repository": {"full_name": pwr.github_repo, "default_branch": "main"},
                    "pull_request": {"number": number, "title": title, "html_url": f"https://github.com/{pwr.github_repo}/pull/{number}",
                                     "user": {"login": user}, "state": state, "merged": merged, "draft": draft, "body": body,
                                     "head": {"ref": branch, "sha": f"{number:040d}"}, "created_at": (now - timedelta(days=1)).isoformat()}}
        github.handle("pull_request", pr_payload("opened", 41, "PWR-2 Fix feedback divider for 3.3 V", "pwr-2-feedback-divider", "sana-pcb"))
        github.handle("pull_request_review", {"action": "submitted", "repository": {"full_name": pwr.github_repo},
                                              "review": {"state": "approved", "user": {"login": "ayesha-k"}, "html_url": f"https://github.com/{pwr.github_repo}/pull/41"},
                                              "pull_request": {"number": 41, "title": "PWR-2 Fix feedback divider for 3.3 V", "html_url": f"https://github.com/{pwr.github_repo}/pull/41"}})
        github.handle("pull_request", pr_payload("closed", 41, "PWR-2 Fix feedback divider for 3.3 V", "pwr-2-feedback-divider", "sana-pcb", merged=True, state="closed"))
        github.handle("pull_request", pr_payload("opened", 44, "Brown-out detection with PVD and log flush", "pwr-4-brownout", "bilal-fw", body="Implements PWR-4."))
        github.handle("workflow_run", {"action": "completed", "repository": {"full_name": pwr.github_repo},
                                       "workflow_run": {"name": "Firmware build + unit tests", "conclusion": "success", "head_sha": f"{44:040d}",
                                                        "html_url": f"https://github.com/{pwr.github_repo}/actions/runs/1001", "head_branch": "pwr-4-brownout"}})
        github.handle("pull_request", pr_payload("opened", 45, "PWR-1 Add USBLC6 ESD protection", "pwr-1-usb-esd", "sana-pcb", draft=True))
        # The brown-out PR moved PWR-4 to review; leave it there so the reviewer sees it.

        # Blocked task
        Task.objects.filter(project=pwr, number=3).update(blocked_reason="Waiting for final USB-C connector footprint from GCT")

        # Time entries for this week and last week
        from apps.timesheets.models import TimeEntry
        today = timezone.localdate()
        monday = today - timedelta(days=today.weekday())
        plan = [("sana", "PWR", 1, [3, 2.5, 4, 3]), ("bilal", "PWR", 4, [4, 3.5, 2, 4]), ("bilal", "SENS", None, [1, 1.5]),
                ("usman", "PWR", 6, [2, 3]), ("ayesha", "PWR", 2, [1.5, 1]), ("haris", "PWR", 4, [1, 0.75, 1.25])]
        for who, key, num, hours in plan:
            proj = Project.objects.get(key=key)
            t = Task.objects.filter(project=proj, number=num).first() if num else None
            for i, h in enumerate(hours):
                for offset in (0, -7):
                    d = monday + timedelta(days=i + offset)
                    if d <= today:
                        TimeEntry.objects.create(user=u[who], project=proj, task=t, date=d, hours=Decimal(str(h)),
                                                 note="" if offset else "Demo entry")

        # Files
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.files.models import Folder
        from apps.files.views import store_upload
        def put(project, path, name, content, user, task=None):
            folder = Folder.get_or_create_path(project, *path, user=user) if path else None
            return store_upload(SimpleUploadedFile(name, content), project=project, folder=folder, user=user, task=task)[0]
        put(pwr, ["Manufacturing", "Rev B"], "PWR-RevB-BOM.csv", sample.read_bytes(), u["sana"])
        put(pwr, ["Test reports"], "RevA-thermal-2A.csv", b"time_min,case_temp_c,ambient_c\n0,25.1,25.0\n10,58.4,25.1\n20,68.9,25.0\n30,71.2,25.0\n", u["usman"])
        notes = b"# Rev B design review\n\nAttendees: Ayesha, Sana, Bilal, Haris\n\n- ESD: USBLC6-2SC6 behind J1 (PWR-1)\n- Feedback divider fixed (PWR-2)\n- Open: connector footprint from GCT\n"
        put(pwr, ["Meeting notes"], "2026-09-22-revB-review.md", notes, u["haris"])
        doc = put(pwr, ["Meeting notes"], "2026-09-22-revB-review.md", notes + b"- Decision: keep 2-layer stack-up\n", u["ayesha"])
        doc.versions.filter(number=2).update(note="Added stack-up decision")
        put(None, ["Procedures"], "Firmware-release-checklist.md", b"# Firmware release\n\n1. Tag in git\n2. CI green\n3. Update CHANGELOG\n", u["bilal"])
        put(None, ["Datasheets"], "TPS62133-notes.txt", b"TPS62133 key limits: VIN 3-17V, 3A, FB 0.8V\n", u["sana"])
        put(pwr, ["Task files", "PWR-4"], "pvd-flush-timing.csv", b"run,pvd_to_flush_ms\n1,2.9\n2,3.1\n3,3.0\n", u["bilal"],
            task=Task.objects.get(project=pwr, number=4))

        # Purchasing and production
        po = PurchaseOrder.objects.create(supplier=s["LCSC"], created_by=u["fatima"], reference="LC-20260918-7731",
                                          expected_date=(now + timedelta(days=5)).date(), shipping_cost=Decimal("18.00"))
        for part, qty in ((Part.objects.get(value="31.6k"), 500), (Part.objects.get(value="4.7uF"), 200)):
            po.lines.create(part=part, quantity=qty, unit_cost=part.unit_cost)
        po.status = PurchaseOrder.Status.ORDERED
        po.ordered_at = now - timedelta(days=4)
        po.save()
        pcb_po = PurchaseOrder.objects.create(supplier=s["JLCPCB"], created_by=u["fatima"], notes="Rev A bare boards")
        pcb_po.lines.create(part=pcb_b, quantity=20, unit_cost=Decimal("1.20"))

        BuildOrder.objects.create(revision=rev_b, quantity=25, manufacturer=s["Lahore Electronics Assembly"], created_by=u["fatima"],
                                  due_date=(now + timedelta(days=30)).date(), serial_prefix="PWRB-26-",
                                  notes="EVT build for field trials. 5 units go to Toronto.")
        done = BuildOrder.objects.create(revision=rev_a, quantity=10, created_by=u["fatima"], status=BuildOrder.Status.COMPLETED,
                                         completed_qty=9, failed_qty=1, stock_consumed=True,
                                         started_at=now - timedelta(days=40), completed_at=now - timedelta(days=33),
                                         notes="Unit 7 failed: solder bridge on U1. Reworked later.")
        log_activity(pwr, f"completed build {done.number}: 9 passed, 1 failed (90% yield)", actor=u["fatima"], url=done.get_absolute_url())

        self.stdout.write(self.style.SUCCESS("Demo data loaded."))
        self.stdout.write(f"Sign in as any of: {', '.join(u)}  —  password: {o['password']}")

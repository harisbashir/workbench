"""Demo data for the hardware side of a product: its boards, board 3D models, mechanical parts and block diagrams.

Used by `manage seed_demo`. Everything is generated in code, so the demo needs no files.
"""
from datetime import timedelta
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone


def _n(i, t, x, y, w, h, label, sub="", color=None, **extra):
    n = {"id": i, "type": t, "x": x, "y": y, "w": w, "h": h, "label": label, "sub": sub}
    if color:
        n["color"] = color
    n.update(extra)
    return n


def _e(i, a, b, kind="signal", label="", arrow="end", **extra):
    e = {"id": i, "from": a, "to": b, "kind": kind, "label": label, "arrow": arrow, "route": "ortho"}
    e.update(extra)
    return e


def system_diagram(power, control, panel):
    nodes = [
        _n("n1", "frame", 40, 40, 900, 330, "Power management unit — enclosure (PETG, IP54)"),
        _n("n2", "board", 90, 130, 200, 110, "Power board", "Buck converter, load switch, supervisor", ref=str(power.pk)),
        _n("n3", "board", 400, 130, 200, 110, "Control board", "STM32G0, logging, USB", ref=str(control.pk), color="blue"),
        _n("n4", "board", 700, 130, 200, 110, "Front panel board", "Display, 2 buttons, status LEDs", ref=str(panel.pk), color="teal"),
        _n("n5", "battery", 90, 440, 150, 70, "Battery pack", "3S Li-ion, 11.1 V, 6 Ah"),
        _n("n6", "ellipse", 400, 440, 170, 70, "Host PC", "Configuration & log download"),
        _n("n7", "connector", 700, 440, 170, 60, "Field sensors", "M12, 4-20 mA ×2"),
        _n("n8", "text", 620, 290, 240, 30, "All inter-board cables: JST PH, 2.0 mm"),
    ]
    edges = [
        _e("e1", "n5", "n2", "power", "VBAT 9–12.6 V"),
        _e("e2", "n2", "n3", "power", "5 V, 2 A"),
        _e("e3", "n3", "n2", "bus", "PMBus / I²C", "both", fromSide="bottom", toSide="bottom"),
        _e("e4", "n3", "n4", "bus", "I²C + INT", "both"),
        _e("e5", "n6", "n3", "highspeed", "USB-C", "both"),
        _e("e6", "n7", "n3", "analog", "4-20 mA"),
    ]
    return {"v": 1, "nodes": nodes, "edges": edges, "page": {"legend": True}}


def power_high_level():
    nodes = [
        _n("n1", "frame", 40, 40, 860, 400, "Power board — Rev B"),
        _n("n2", "connector", 70, 110, 140, 56, "J1 USB-C", "5–20 V PD sink"),
        _n("n3", "connector", 70, 230, 140, 56, "J3 Battery", "JST PH 2-pin"),
        _n("n4", "power", 270, 105, 160, 64, "Input protection", "TVS, reverse polarity, 3 A fuse"),
        _n("n5", "power", 270, 225, 160, 64, "Ideal diode OR", "LM66200"),
        _n("n6", "power", 490, 105, 160, 64, "Buck converter", "TPS62133 → 5 V, 3 A"),
        _n("n7", "power", 490, 225, 160, 64, "LDO", "3.3 V, 300 mA"),
        _n("n8", "mcu", 490, 330, 160, 90, "Supervisor MCU", "STM32G031K8"),
        _n("n9", "block", 720, 105, 150, 60, "Load switch", "AO3401A, 2 A", color="red"),
        _n("n10", "connector", 720, 230, 150, 56, "J2 To control board", "5 V, PMBus, EN"),
        _n("n11", "sensor", 720, 345, 150, 50, "Current sense", "INA219, I²C 0x40"),
    ]
    edges = [
        _e("e1", "n2", "n4", "power", "VBUS"), _e("e2", "n3", "n5", "power", "VBAT"), _e("e3", "n4", "n5", "power", ""),
        _e("e4", "n5", "n6", "power", "VIN"), _e("e5", "n6", "n9", "power", "5V0"), _e("e6", "n6", "n7", "power", ""),
        _e("e7", "n7", "n8", "power", "3V3"), _e("e8", "n9", "n10", "power", "5V_OUT"),
        _e("e9", "n8", "n10", "bus", "PMBus", "both"), _e("e10", "n8", "n11", "bus", "I²C", "both"),
        _e("e11", "n8", "n9", "signal", "EN", fromSide="right", toSide="bottom"),
    ]
    return {"v": 1, "nodes": nodes, "edges": edges, "page": {"legend": True}}


def power_detailed():
    d = power_high_level()
    for n in d["nodes"]:
        if n["id"] == "n6":
            n["sub"] = "TPS62133RGTR, 2.5 MHz, L1 4.7 µH, 2×22 µF out"
            n["h"] = 74
        if n["id"] == "n8":
            n["sub"] = "STM32G031K8T6, LQFP-32, 64 MHz, 64 kB"
        if n["id"] == "n4":
            n["sub"] = "SMBJ20A TVS, Q2 PMOS, F1 3 A PTC"
    d["nodes"][0]["label"] = "Power board — Rev B (detailed)"
    d["nodes"][0]["h"] = 470
    d["nodes"].append(_n("n12", "memory", 270, 350, 140, 70, "EEPROM", "24C02, I²C 0x50, calibration"))
    d["nodes"].append(_n("n13", "connector", 70, 360, 140, 56, "J4 SWD", "Tag-Connect TC2030"))
    d["edges"] += [_e("e12", "n8", "n12", "bus", "I²C", "both"), _e("e13", "n13", "n8", "signal", "SWDIO, SWCLK, NRST", "both")]
    return d


def control_high_level():
    nodes = [
        _n("n1", "frame", 40, 40, 760, 380, "Control board — Rev A"),
        _n("n2", "connector", 70, 110, 140, 56, "J1 From power board", "5 V, PMBus"),
        _n("n3", "power", 270, 105, 150, 64, "3.3 V regulator", "TLV62569"),
        _n("n4", "mcu", 470, 160, 170, 100, "MCU", "STM32G071RB"),
        _n("n5", "memory", 680, 80, 100, 70, "Flash", "W25Q64, 8 MB"),
        _n("n6", "connector", 680, 200, 110, 56, "USB-C", "Device, 12 Mbit/s"),
        _n("n7", "sensor", 470, 330, 170, 50, "4-20 mA inputs", "2× shunt + ADC"),
        _n("n8", "connector", 70, 300, 140, 56, "J5 Front panel", "I²C, INT, 3V3"),
    ]
    edges = [_e("e1", "n2", "n3", "power", "5V0"), _e("e2", "n3", "n4", "power", "3V3"),
             _e("e3", "n4", "n5", "bus", "QSPI", "both"), _e("e4", "n4", "n6", "highspeed", "USB FS", "both"),
             _e("e5", "n7", "n4", "analog", "ADC1 ch 4, 5"), _e("e6", "n4", "n8", "bus", "I²C2", "both"),
             _e("e7", "n2", "n4", "bus", "PMBus", "both", fromSide="bottom", toSide="left")]
    return {"v": 1, "nodes": nodes, "edges": edges, "page": {"legend": True}}


def seed(pwr, users, rev_a, rev_b, now=None):
    """Adds boards, 3D models, mechanical parts and diagrams to the PWR demo product. Returns the new boards."""
    from apps.cad import samples
    from apps.design import demo_board
    from apps.design.models import DesignFile
    from apps.diagrams import geometry
    from apps.diagrams.models import Diagram, DiagramComment, DiagramVersion
    from apps.mechanical.models import MechanicalFile, MechanicalPart
    from apps.projects.models import Board, Revision

    now = now or timezone.now()
    u = users
    power = rev_b.board
    power.name, power.kind, power.code = "Power board", Board.Kind.POWER, "PWR-PB"
    power.description = "Takes USB-C or battery power, makes the 5 V and 3.3 V rails and switches the load. An STM32G0 supervises it."
    power.save()
    control = Board.objects.create(project=pwr, name="Control board", kind=Board.Kind.CONTROL, code="PWR-CB", order=1,
                                   description="The main controller: logs the field sensors, talks to the host over USB and drives the front panel.")
    panel = Board.objects.create(project=pwr, name="Front panel board", kind=Board.Kind.DISPLAY, code="PWR-FP", order=2,
                                 description="Small display, two buttons and status LEDs, connected to the control board over I²C.")
    ctrl_a = Revision.objects.create(project=pwr, board=control, name="Rev A", status=Revision.Status.DESIGN,
                                     target_date=(now + timedelta(days=30)).date(), notes="First spin of the control board.")
    panel_a = Revision.objects.create(project=pwr, board=panel, name="Rev A", status=Revision.Status.DESIGN,
                                      target_date=(now + timedelta(days=40)).date())
    for r in (ctrl_a, panel_a):
        r.add_default_checks()

    # Board 3D models, as exported from KiCad
    parts = [(p[0], p[3], p[4], p[2], p[5]) for p in demo_board.PARTS]
    DesignFile.store(rev_b, SimpleUploadedFile("pwr-board-revB.wrl", samples.board_vrml(parts)), u["sana"],
                     note="KiCad → File → Export → VRML", background=False)
    step = Path(__file__).resolve().parents[1] / "cad" / "tests" / "fixtures" / "board_assembly.step"
    if step.exists():
        DesignFile.store(ctrl_a, SimpleUploadedFile("ctrl-board-revA.step", step.read_bytes()), u["bilal"],
                         note="KiCad → File → Export → STEP", background=False)

    # Mechanical parts
    enc = MechanicalPart.objects.create(project=pwr, name="Main enclosure", kind=MechanicalPart.Kind.ENCLOSURE, revision="B",
                                        status=MechanicalPart.Status.PROTOTYPE, process=MechanicalPart.Process.FDM,
                                        material="PETG", finish="Matte black, 0.2 mm layers", part_number="MEC-1001",
                                        supplier="JLC3DP", created_by=u["usman"],
                                        notes="Base and lid, 4× M3 heat-set inserts. Rev B moves the USB cut-out 2 mm up.")
    enc.boards.add(power, control, panel)
    MechanicalFile.store(enc, SimpleUploadedFile("pmu-enclosure-assembly-revB.3mf", samples.enclosure_assembly()), u["usman"],
                         note="Assembly with boards for fit check", background=False)
    MechanicalFile.store(enc, SimpleUploadedFile("pmu-enclosure-revB.SLDASM", b"(SolidWorks assembly placeholder for the demo)"),
                         u["usman"], note="SolidWorks source", background=False)
    bezel = MechanicalPart.objects.create(project=pwr, name="Front bezel", kind=MechanicalPart.Kind.PANEL, revision="A",
                                          status=MechanicalPart.Status.PROTOTYPE, process=MechanicalPart.Process.SLA,
                                          material="Tough resin", finish="White", part_number="MEC-1002", created_by=u["usman"])
    bezel.boards.add(panel)
    MechanicalFile.store(bezel, SimpleUploadedFile("front-bezel-revA.stl", samples.bezel_stl()), u["usman"], background=False)
    bracket = MechanicalPart.objects.create(project=pwr, name="Battery bracket", kind=MechanicalPart.Kind.BRACKET, revision="A",
                                            status=MechanicalPart.Status.CONCEPT, process=MechanicalPart.Process.SHEET,
                                            material="Aluminium 5052, 1.5 mm", created_by=u["usman"])
    MechanicalFile.store(bracket, SimpleUploadedFile("battery-bracket.f3d", b"(Fusion 360 design placeholder for the demo)"),
                         u["usman"], note="Fusion 360 source — STEP export to follow", background=False)

    # Block diagrams
    def diagram(name, level, board, versions, author, status="draft", approver=None):
        d = Diagram.objects.create(project=pwr, board=board, name=name, level=level, created_by=author, updated_by=author,
                                   current_version=len(versions), status=status)
        for i, (data, note, days) in enumerate(versions, start=1):
            v = DiagramVersion.objects.create(diagram=d, number=i, data=geometry.clean(data), note=note, created_by=author)
            DiagramVersion.objects.filter(pk=v.pk).update(created_at=now - timedelta(days=days))
        if status == "approved":
            d.approved_version, d.approved_by, d.approved_at = len(versions), approver, now - timedelta(days=versions[-1][2] - 1)
            d.save()
        return d

    sysd = diagram("Power management unit — system", Diagram.Level.SYSTEM, None,
                   [(system_diagram(power, control, panel), "Boards and interconnect agreed in the kick-off", 20)],
                   u["haris"], "approved", u["ayesha"])
    hl = power_high_level()
    hl_v1 = {**hl, "nodes": [n for n in hl["nodes"] if n["id"] != "n11"], "edges": [e for e in hl["edges"] if e["to"] != "n11"]}
    ph = diagram("Power board — high-level", Diagram.Level.HIGH, power,
                 [(hl_v1, "First draft", 14), (hl, "Added INA219 current sense", 9)], u["sana"], "approved", u["ayesha"])
    DiagramComment.objects.create(diagram=ph, version=1, node_id="n9", node_label="Load switch", author=u["ayesha"],
                                  text="We need to measure load current for the logger — add a current sense block.", resolved=True)
    pd = diagram("Power board — detailed", Diagram.Level.DETAILED, power,
                 [(power_detailed(), "Part numbers and rails for Rev B", 2)], u["sana"], "review")
    DiagramComment.objects.create(diagram=pd, version=1, node_id="n6", node_label="Buck converter", author=u["ayesha"],
                                  text="2× 22 µF is marginal at 3 A with the 2.5 MHz setting. Check the ripple spec, maybe 3×.")
    DiagramComment.objects.create(diagram=pd, version=1, node_id="n12", node_label="EEPROM", author=u["haris"],
                                  text="Address 0x50 clashes with nothing on this bus, good. Please write the page size in the detail.")
    diagram("Control board — high-level", Diagram.Level.HIGH, control, [(control_high_level(), "Created", 5)], u["bilal"])
    for d in (sysd, ph, pd):
        Diagram.objects.filter(pk=d.pk).update(updated_at=now - timedelta(days=1))
    return power, control, panel

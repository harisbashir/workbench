"""Starting points for new diagrams, so nobody faces an empty page."""


def _n(i, t, x, y, label, sub="", w=None, h=None, color=None, **extra):
    from .geometry import SHAPES
    s = SHAPES[t]
    n = {"id": f"n{i}", "type": t, "x": x, "y": y, "w": w or s["w"], "h": h or s["h"], "label": label, "sub": sub}
    if color:
        n["color"] = color
    n.update(extra)
    return n


def _e(i, a, b, kind="signal", label="", arrow="end", **extra):
    e = {"id": f"e{i}", "from": a, "to": b, "kind": kind, "label": label, "arrow": arrow, "route": "ortho"}
    e.update(extra)
    return e


def blank():
    return {"v": 1, "nodes": [], "edges": [], "page": {"legend": True}}


def board_high_level(name="Board"):
    """A typical microcontroller board: power path, MCU, memory, sensors and interfaces."""
    nodes = [
        _n(1, "frame", 40, 40, name, w=820, h=420),
        _n(2, "connector", 70, 110, "Power input", "USB-C / 12 V barrel"),
        _n(3, "power", 250, 105, "Protection", "Reverse polarity, TVS, fuse"),
        _n(4, "power", 250, 215, "Regulator", "3.3 V, 500 mA"),
        _n(5, "mcu", 440, 170, "Microcontroller", "MCU part number"),
        _n(6, "memory", 680, 90, "Flash", "SPI NOR"),
        _n(7, "sensor", 680, 210, "Sensors", "I²C"),
        _n(8, "connector", 680, 320, "I/O connector", "UART / GPIO"),
        _n(9, "connector", 440, 340, "Debug", "SWD / JTAG"),
        _n(10, "ellipse", 70, 330, "Host / user", ""),
    ]
    edges = [
        _e(1, "n2", "n3", "power", "VIN"),
        _e(2, "n3", "n4", "power", ""),
        _e(3, "n4", "n5", "power", "3V3"),
        _e(4, "n5", "n6", "bus", "SPI", "both"),
        _e(5, "n5", "n7", "bus", "I²C", "both"),
        _e(6, "n5", "n8", "bus", "UART", "both"),
        _e(7, "n9", "n5", "signal", "SWD", "both"),
        _e(8, "n10", "n2", "mechanical", "cable"),
    ]
    return {"v": 1, "nodes": nodes, "edges": edges, "page": {"legend": True}}


def system(boards, product="Product"):
    """One block per board of the product inside an enclosure frame, ready to connect."""
    nodes, x = [], 90
    count = max(len(boards), 1)
    frame_w = max(560, 60 + count * 230)
    nodes.append(_n(1, "frame", 40, 60, f"{product} enclosure", w=frame_w, h=260))
    for i, b in enumerate(boards, start=2):
        nodes.append(_n(i, "board", x, 130, b.name, b.get_kind_display(), ref=str(b.pk)))
        x += 230
    nodes.append(_n(len(nodes) + 1, "ellipse", 40 + frame_w / 2 - 65, 380, "User / host", "Buttons, display, PC"))
    nodes.append(_n(len(nodes) + 1, "battery", 40 + frame_w + 60, 150, "Power source", "Mains / battery"))
    return {"v": 1, "nodes": nodes, "edges": [], "page": {"legend": True}}

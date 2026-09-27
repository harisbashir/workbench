"""A made-up but realistic 2-layer board ("PWR Rev B") in KiCad 7/8 output style.

Used for the demo data and the tests: Gerber X2 files with RoundRect pads,
regions, arcs and clear polarity, Excellon drill files (plated and not, with
slots), a .gbrjob file and a KiCad pick-and-place (.pos CSV) file.
"""
import io
import json
import math
import zipfile

W, H, R = 50.0, 32.0, 2.0   # board size and corner radius (mm)

# Stroke font on a 4×6 grid: each character is a list of polylines.
FONT = {
    "A": [[(0, 0), (0, 4), (2, 6), (4, 4), (4, 0)], [(0, 3), (4, 3)]],
    "B": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)], [(3, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "C": [[(4, 1), (3, 0), (1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5)]],
    "D": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 1), (3, 0), (0, 0)]],
    "E": [[(4, 0), (0, 0), (0, 6), (4, 6)], [(0, 3), (3, 3)]],
    "F": [[(0, 0), (0, 6), (4, 6)], [(0, 3), (3, 3)]],
    "G": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 3), (2, 3)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 0), (3, 0)], [(2, 0), (2, 6)], [(1, 6), (3, 6)]],
    "J": [[(0, 1), (1, 0), (2, 0), (3, 1), (3, 6)]],
    "K": [[(0, 0), (0, 6)], [(4, 6), (0, 2)], [(1, 3), (4, 0)]],
    "L": [[(0, 6), (0, 0), (4, 0)]],
    "M": [[(0, 0), (0, 6), (2, 3), (4, 6), (4, 0)]],
    "N": [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    "O": [[(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)]],
    "P": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)]],
    "Q": [[(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)], [(2, 2), (4, 0)]],
    "R": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)], [(2, 3), (4, 0)]],
    "S": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3), (1, 3), (0, 4), (0, 5), (1, 6), (3, 6), (4, 5)]],
    "T": [[(0, 6), (4, 6)], [(2, 6), (2, 0)]],
    "U": [[(0, 6), (0, 1), (1, 0), (3, 0), (4, 1), (4, 6)]],
    "V": [[(0, 6), (2, 0), (4, 6)]],
    "W": [[(0, 6), (1, 0), (2, 3), (3, 0), (4, 6)]],
    "X": [[(0, 0), (4, 6)], [(0, 6), (4, 0)]],
    "Y": [[(0, 6), (2, 3), (4, 6)], [(2, 3), (2, 0)]],
    "Z": [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    "0": [[(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)], [(0, 1), (4, 5)]],
    "1": [[(1, 5), (2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "2": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0), (4, 0)]],
    "3": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)], [(1, 3), (3, 3)]],
    "4": [[(3, 0), (3, 6), (0, 2), (4, 2)]],
    "5": [[(4, 6), (0, 6), (0, 3), (3, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "6": [[(3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)]],
    "7": [[(0, 6), (4, 6), (1, 0)]],
    "8": [[(1, 3), (0, 4), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (1, 3), (0, 2), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3)]],
    "9": [[(1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3), (4, 3)]],
    "-": [[(1, 3), (3, 3)]],
    ".": [[(2, 0), (2, 0.3)]],
    "+": [[(0, 3), (4, 3)], [(2, 1), (2, 5)]],
}

# (ref, value, package, x, y, rotation, side, pads) — pads: list of (dx, dy, w, h, shape) relative, before rotation
def _two(pitch, w, h):
    return [(-pitch / 2, 0, w, h, "rr"), (pitch / 2, 0, w, h, "rr")]


def _sot23(n=3):
    if n == 3:
        return [(-0.95, -1.1, 0.6, 1.1, "rr"), (0.95, -1.1, 0.6, 1.1, "rr"), (0, 1.1, 0.6, 1.1, "rr")]
    return [(dx, dy, 0.6, 1.1, "rr") for dy in (-1.1, 1.1) for dx in (-0.95, 0, 0.95)]


def _qfn16():
    pads = []
    for side in range(4):
        for k in range(4):
            off = (k - 1.5) * 0.5
            if side == 0:
                pads.append((-1.45, off, 0.8, 0.25, "rr"))
            elif side == 1:
                pads.append((off, -1.45, 0.25, 0.8, "rr"))
            elif side == 2:
                pads.append((1.45, off, 0.8, 0.25, "rr"))
            else:
                pads.append((off, 1.45, 0.25, 0.8, "rr"))
    pads.append((0, 0, 1.7, 1.7, "rect"))
    return pads


def _lqfp32():
    pads = []
    for k in range(8):
        off = (k - 3.5) * 0.8
        pads += [(-4.25, off, 1.5, 0.5, "rr"), (4.25, off, 1.5, 0.5, "rr"), (off, -4.25, 0.5, 1.5, "rr"), (off, 4.25, 0.5, 1.5, "rr")]
    return pads


PARTS = [
    ("J1", "USB_C_Receptacle", "USB_C_GCT_USB4105", 3.2, 16.0, 270, "top",
     [((k - 5.5) * 0.5, 1.2, 0.3, 1.1, "rr") for k in range(12)]),
    ("U3", "USBLC6-2SC6", "SOT-23-6", 9.5, 16.0, 90, "top", _sot23(6)),
    ("C3", "22uF", "C_1206_3216Metric", 10.5, 26.5, 0, "top", _two(2.9, 1.15, 1.8)),
    ("C4", "22uF", "C_1206_3216Metric", 10.5, 5.5, 0, "top", _two(2.9, 1.15, 1.8)),
    ("U1", "TPS62133", "Texas_RGT0016C", 17.5, 21.0, 0, "top", _qfn16()),
    ("L1", "4.7uH", "L_Bourns_SRN6045TA", 17.5, 9.0, 90, "top", _two(4.6, 2.0, 5.2)),
    ("C1", "100nF", "C_0603_1608Metric", 14.0, 25.5, 0, "top", _two(1.55, 0.9, 0.95)),
    ("C2", "100nF", "C_0603_1608Metric", 21.5, 25.5, 0, "top", _two(1.55, 0.9, 0.95)),
    ("R3", "100k", "R_0603_1608Metric", 22.0, 21.5, 90, "top", _two(1.55, 0.9, 0.95)),
    ("R4", "31.6k", "R_0603_1608Metric", 24.0, 21.5, 90, "top", _two(1.55, 0.9, 0.95)),
    ("C7", "10uF", "C_0805_2012Metric", 23.0, 12.0, 90, "top", _two(1.9, 1.0, 1.45)),
    ("C8", "4.7uF", "C_0805_2012Metric", 25.5, 12.0, 90, "top", _two(1.9, 1.0, 1.45)),
    ("Q1", "AO3401A", "SOT-23", 24.0, 27.5, 0, "top", _sot23(3)),
    ("U2", "STM32G031K8", "LQFP-32_7x7mm_P0.8mm", 34.0, 16.0, 45, "top", _lqfp32()),
    ("C5", "100nF", "C_0603_1608Metric", 34.0, 25.0, 0, "top", _two(1.55, 0.9, 0.95)),
    ("C6", "100nF", "C_0603_1608Metric", 34.0, 7.0, 0, "top", _two(1.55, 0.9, 0.95)),
    ("C9", "100nF", "C_0603_1608Metric", 28.0, 16.0, 90, "top", _two(1.55, 0.9, 0.95)),
    ("R1", "10k", "R_0603_1608Metric", 40.5, 25.0, 0, "top", _two(1.55, 0.9, 0.95)),
    ("R2", "10k", "R_0603_1608Metric", 40.5, 7.0, 0, "top", _two(1.55, 0.9, 0.95)),
    ("R5", "5.1k", "R_0603_1608Metric", 7.5, 22.0, 90, "top", _two(1.55, 0.9, 0.95)),
    ("R6", "5.1k", "R_0603_1608Metric", 7.5, 10.0, 90, "top", _two(1.55, 0.9, 0.95)),
    ("R7", "0R", "R_0603_1608Metric", 28.0, 25.5, 0, "top", _two(1.55, 0.9, 0.95)),
    ("D1", "LED_Green", "LED_0603_1608Metric", 41.5, 28.2, 0, "top", _two(1.55, 0.9, 0.95)),
    ("J2", "Conn_01x04", "JST_PH_B4B-PH-K_1x04_P2.00mm_Vertical", 46.0, 13.0, 90, "top", "tht"),
]
J2_PADS = [((k - 1.5) * 2.0, 0.0) for k in range(4)]
VIAS = [(13.0, 16.0), (15.0, 13.5), (20.5, 16.5), (26.5, 19.0), (29.5, 9.5), (39.0, 12.0), (39.0, 20.0), (42.5, 16.0),
        (12.0, 11.0), (12.0, 21.0), (31.0, 26.0), (37.5, 26.0), (37.5, 6.0), (20.0, 5.0)]
MOUNT = [(4.0, 4.0), (46.0, 4.0), (4.0, 28.0), (46.0, 28.0)]
TESTPOINTS = [("TP1", 30.0, 4.0, "GND"), ("TP2", 42.0, 4.0, "3V3"), ("TP3", 30.0, 28.5, "SWD")]
TRACKS = [  # (width, [points]) on top copper
    (0.5, [(4.4, 13.2), (7.0, 13.2), (8.6, 14.8)]), (0.5, [(4.4, 18.8), (7.0, 18.8), (8.6, 17.2)]),
    (0.25, [(10.4, 17.0), (13.0, 17.0), (15.5, 19.5), (16.05, 19.5)]),
    (0.8, [(11.95, 26.5), (15.0, 26.5), (16.5, 23.0), (16.75, 22.45)]),
    (0.8, [(11.95, 5.5), (15.0, 5.5), (17.5, 6.7)]), (0.8, [(17.5, 11.3), (17.5, 13.5), (18.2, 16.0), (18.25, 19.55)]),
    (0.25, [(18.95, 21.25), (22.0, 22.3)]), (0.25, [(22.0, 20.7), (24.0, 20.7)]), (0.5, [(19.5, 23.0), (21.5, 25.5), (20.7, 25.5)]),
    (0.5, [(23.0, 12.95), (25.5, 12.95)]), (0.5, [(23.0, 11.05), (23.0, 9.0), (18.5, 9.0)]),
    (0.25, [(25.5, 12.95), (27.8, 15.2), (28.0, 15.2)]), (0.25, [(28.0, 16.8), (29.7, 16.8), (30.6, 16.0)]),
    (0.25, [(24.95, 26.4), (27.2, 25.5)]), (0.25, [(28.8, 25.5), (32.0, 22.5)]),
    (0.25, [(41.3, 25.0), (42.3, 26.5), (42.3, 28.2)]), (0.25, [(39.7, 25.0), (37.8, 21.0)]), (0.25, [(39.7, 7.0), (37.8, 11.0)]),
    (0.25, [(38.0, 16.5), (42.5, 16.0), (46.0, 16.0)]), (0.25, [(38.0, 15.5), (42.0, 14.0), (46.0, 14.0)]),
    (0.25, [(36.5, 12.0), (39.0, 12.0)]), (0.25, [(36.5, 20.0), (39.0, 20.0)]), (0.25, [(34.8, 25.0), (37.5, 26.0)]),
    (0.25, [(34.8, 7.0), (37.5, 6.0)]), (0.5, [(33.2, 25.0), (31.0, 26.0), (30.0, 28.5)]),
]
BOTTOM_TRACKS = [(0.5, [(13.0, 16.0), (15.0, 13.5)]), (0.25, [(20.5, 16.5), (26.5, 19.0)]), (0.25, [(29.5, 9.5), (39.0, 12.0)]),
                 (0.25, [(39.0, 20.0), (42.5, 16.0)]), (0.5, [(12.0, 11.0), (12.0, 21.0)]), (0.25, [(31.0, 26.0), (37.5, 26.0)])]


def rot(dx, dy, deg):
    a = math.radians(deg)
    return dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)


def pads_of(part):
    ref, _v, _p, x, y, r, side, pads = part
    if pads == "tht":
        return []
    out = []
    for dx, dy, w, h, shape in pads:
        px, py = rot(dx, dy, r)
        if r % 180 == 90:
            w, h = h, w
        out.append((x + px, y + py, w, h, shape, r if r % 90 else 0))
    return out


class G:
    """A tiny Gerber writer (mm, 4.6 format)."""

    def __init__(self, function, polarity="Positive"):
        self.lines = [
            "%TF.GenerationSoftware,KiCad,Pcbnew,8.0.4*%",
            "%TF.CreationDate,2026-09-22T10:14:03+05:00*%",
            "%TF.ProjectId,pwr-board,70777262-6f61-7264-0000-000000000000,rev?*%",
            "%TF.SameCoordinates,Original*%",
            f"%TF.FileFunction,{function}*%",
            f"%TF.FilePolarity,{polarity}*%",
            "%FSLAX46Y46*%",
            "G04 Gerber Fmt 4.6, Leading zero omitted, Abs format (unit mm)*",
            "G04 Created by KiCad (PCBNEW 8.0.4) date 2026-09-22 10:14:03*",
            "%MOMM*%",
            "%LPD*%",
            "G01*",
            "G04 APERTURE LIST*",
        ]
        self.apertures = {}
        self.body = []
        self.macro = False
        self.cur = None

    @staticmethod
    def n(v):
        return str(int(round(v * 1_000_000)))

    def ap(self, key, definition):
        if key not in self.apertures:
            code = 10 + len(self.apertures)
            self.apertures[key] = code
            if definition.startswith("RoundRect") and not self.macro:
                self.macro = True
                self.lines += [
                    "%AMRoundRect*", "0 Rectangle with rounded corners*", "0 $1 Rounding radius*",
                    "0 $2 $3 $4 $5 $6 $7 $8 $9 X,Y pos of 4 corners*", "0 Add a 4 corners polygon primitive as box body*",
                    "4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*", "0 Add four circle primitives for the rounded corners*",
                    "1,1,$1+$1,$2,$3*", "1,1,$1+$1,$4,$5*", "1,1,$1+$1,$6,$7*", "1,1,$1+$1,$8,$9*",
                    "0 Add four rect primitives between the rounded corners*",
                    "20,1,$1+$1,$2,$3,$4,$5,0*", "20,1,$1+$1,$4,$5,$6,$7,0*", "20,1,$1+$1,$6,$7,$8,$9,0*", "20,1,$1+$1,$8,$9,$2,$3,0*%",
                ]
            self.lines.append(f"%ADD{code}{definition}*%")
        return self.apertures[key]

    def use(self, code):
        if self.cur != code:
            self.body.append(f"D{code}*")
            self.cur = code

    def circle_ap(self, d):
        return self.ap(("C", round(d, 4)), f"C,{d:.6f}")

    def pad(self, x, y, w, h, shape="rr", angle=0):
        if shape == "circle":
            code = self.circle_ap(w)
        elif shape == "oval":
            code = self.ap(("O", round(w, 4), round(h, 4)), f"O,{w:.6f}X{h:.6f}")
        elif shape == "rect" and not angle:
            code = self.ap(("R", round(w, 4), round(h, 4)), f"R,{w:.6f}X{h:.6f}")
        else:
            r = 0.25 * min(w, h)
            corners = [(-w / 2 + r, -h / 2 + r), (w / 2 - r, -h / 2 + r), (w / 2 - r, h / 2 - r), (-w / 2 + r, h / 2 - r)]
            corners = [rot(cx, cy, angle) for cx, cy in corners]
            params = "X".join(f"{v:.6f}" for c in corners for v in c)
            code = self.ap(("RR", round(w, 4), round(h, 4), angle), f"RoundRect,{r:.6f}X{params}")
        self.use(code)
        self.body.append(f"X{self.n(x)}Y{self.n(y)}D03*")

    def line(self, pts, width):
        self.use(self.circle_ap(width))
        self.body.append(f"X{self.n(pts[0][0])}Y{self.n(pts[0][1])}D02*")
        for x, y in pts[1:]:
            self.body.append(f"X{self.n(x)}Y{self.n(y)}D01*")

    def arc(self, start, end, centre, width, ccw=True):
        self.use(self.circle_ap(width))
        self.body += ["G75*", f"X{self.n(start[0])}Y{self.n(start[1])}D02*", "G03*" if ccw else "G02*",
                      f"X{self.n(end[0])}Y{self.n(end[1])}I{self.n(centre[0] - start[0])}J{self.n(centre[1] - start[1])}D01*", "G01*"]

    def region(self, pts):
        self.body.append("G36*")
        self.body.append(f"X{self.n(pts[0][0])}Y{self.n(pts[0][1])}D02*")
        for x, y in pts[1:] + [pts[0]]:
            self.body.append(f"X{self.n(x)}Y{self.n(y)}D01*")
        self.body.append("G37*")

    def polarity(self, dark):
        self.body.append("%LPD*%" if dark else "%LPC*%")

    def text(self, s, x, y, size=1.0, width=0.15, mirror=False):
        k = size / 6.0
        cx = x
        for ch in s.upper():
            if ch in FONT:
                for stroke in FONT[ch]:
                    pts = [((cx + px * k) if not mirror else (cx - px * k), y + py * k) for px, py in stroke]
                    if len(pts) == 1:
                        pts = pts * 2
                    self.line(pts, width)
            cx += (5.2 * k) * (-1 if mirror else 1)

    def render(self):
        return "\n".join(self.lines + ["G04 APERTURE END LIST*"] + self.body + ["M02*"]) + "\n"


def outline(g, width=0.05):
    r = R
    g.line([(r, 0), (W - r, 0)], width)
    g.arc((W - r, 0), (W, r), (W - r, r), width)
    g.line([(W, r), (W, H - r)], width)
    g.arc((W, H - r), (W - r, H), (W - r, H - r), width)
    g.line([(W - r, H), (r, H)], width)
    g.arc((r, H), (0, H - r), (r, H - r), width)
    g.line([(0, H - r), (0, r)], width)
    g.arc((0, r), (r, 0), (r, r), width)


def build_files():
    """Returns {file name: bytes} for the whole fabrication package."""
    stem = "pwr-board"
    f_cu, b_cu = G("Copper,L1,Top"), G("Copper,L2,Bot")
    f_mask, b_mask = G("Soldermask,Top", "Negative"), G("Soldermask,Bot", "Negative")
    f_silk, b_silk = G("Legend,Top"), G("Legend,Bot")
    f_paste = G("Paste,Top")
    edge = G("Profile,NP")

    # Ground pour on the bottom, with clearances cut out (clear polarity), then pads on top of it.
    inset = 0.5
    b_cu.region([(inset + 1.5, inset), (W - inset - 1.5, inset), (W - inset, inset + 1.5), (W - inset, H - inset - 1.5),
                 (W - inset - 1.5, H - inset), (inset + 1.5, H - inset), (inset, H - inset - 1.5), (inset, inset + 1.5)])
    b_cu.polarity(False)
    for x, y in MOUNT:
        b_cu.pad(x, y, 4.2, 4.2, "circle")
    for x, y in VIAS[:10]:
        b_cu.pad(x, y, 1.2, 1.2, "circle")
    for w, pts in BOTTOM_TRACKS:
        b_cu.line(pts, w + 0.6)
    for k, (dx, dy) in enumerate(J2_PADS):
        px, py = rot(dx, dy, 90)
        b_cu.pad(46.0 + px, 13.0 + py, 2.0, 2.4, "oval")
    b_cu.pad(3.2, 16.0, 3.0, 10.0, "rect")
    b_cu.polarity(True)
    # Top copper pour around the regulator (a region with an arc-cornered outline)
    f_cu.region([(13.0, 29.5), (27.0, 29.5), (27.0, 24.0), (25.5, 23.0), (20.5, 23.0), (19.5, 24.0), (13.0, 24.0)])
    f_cu.polarity(False)
    for part in PARTS:
        for x, y, w, h, shape, ang in pads_of(part):
            if 12.9 < x < 27.1 and 23.9 < y < 29.6:
                f_cu.pad(x, y, w + 0.5, h + 0.5, "rect" if not ang else "rr", ang)
    f_cu.polarity(True)

    for part in PARTS:
        ref, _val, _pkg, x, y, r, side, _pads = part
        for px, py, w, h, shape, ang in pads_of(part):
            f_cu.pad(px, py, w, h, shape, ang)
            f_mask.pad(px, py, w + 0.1, h + 0.1, shape, ang)
            f_paste.pad(px, py, w, h, shape, ang)
    # USB-C shell tabs (plated slots) and J2 through-hole pads, on both sides
    for sx in (-4.32, 4.32):
        for sy, h in ((-1.15, 2.0), (2.85, 1.6)):
            x, y = 3.2 + sy, 16.0 + sx
            for g, grow in ((f_cu, 0), (b_cu, 0), (f_mask, 0.1), (b_mask, 0.1)):
                g.pad(x, y, h + grow, 1.0 + grow, "oval")
    for k, (dx, dy) in enumerate(J2_PADS):
        px, py = rot(dx, dy, 90)
        shape = "rect" if k == 0 else "oval"
        for g, grow in ((f_cu, 0), (b_cu, 0), (f_mask, 0.1), (b_mask, 0.1)):
            g.pad(46.0 + px, 13.0 + py, 1.2 + grow, 1.75 + grow, shape if shape != "rect" else "rr")
    for x, y in VIAS:
        f_cu.pad(x, y, 0.6, 0.6, "circle")
        b_cu.pad(x, y, 0.6, 0.6, "circle")
    for _ref, x, y, _label in TESTPOINTS:
        b_cu.pad(x, y, 1.5, 1.5, "circle")
        b_mask.pad(x, y, 1.6, 1.6, "circle")
    for w, pts in TRACKS:
        f_cu.line(pts, w)
    for w, pts in BOTTOM_TRACKS:
        b_cu.line(pts, w)

    # Silkscreen
    for part in PARTS:
        ref, _val, _pkg, x, y, r, side, pads = part
        if pads == "tht":
            bw, bh = 3.0, 9.8
            f_silk.line([(x - bw / 2, y - bh / 2), (x + bw / 2, y - bh / 2), (x + bw / 2, y + bh / 2), (x - bw / 2, y + bh / 2), (x - bw / 2, y - bh / 2)], 0.12)
            f_silk.text(ref, x - 2.0, y + bh / 2 + 0.6, 1.0)
            continue
        ps = pads_of(part)
        xs = [p[0] - p[2] / 2 for p in ps] + [p[0] + p[2] / 2 for p in ps]
        ys = [p[1] - p[3] / 2 for p in ps] + [p[1] + p[3] / 2 for p in ps]
        if ref.startswith(("U", "L", "J")):
            m = 0.35
            x0, x1, y0, y1 = min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m
            f_silk.line([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], 0.12)
            if ref.startswith("U"):
                f_silk.line([(x0 - 0.4, y1 + 0.1), (x0 - 0.4, y1 + 0.2)], 0.3)
            ty = y1 + 0.5
        else:
            ty = max(ys) + 0.35
        f_silk.text(ref, x - len(ref) * 0.45, ty, 0.9, 0.13)
    f_silk.text("PWR-B REV B", 21.5, 1.0, 1.4, 0.18)
    f_silk.text("USB", 3.6, 24.3, 0.9, 0.13)
    f_silk.text("3V3 GND SWD", 30.8, 29.9, 0.9, 0.13)
    b_silk.text("WORKBENCH DEMO", 36.0, 14.0, 1.4, 0.18, mirror=True)
    for ref, x, y, label in TESTPOINTS:
        b_silk.text(label, x - 1.3, y - 0.45, 0.9, 0.13, mirror=True)

    outline(edge)

    # Drill files
    def drill(function, tools, hits, slots=()):
        lines = ["M48", "; DRILL file {KiCad 8.0.4} date 2026-09-22 10:14:03", "; FORMAT={-:-/ absolute / metric / decimal}",
                 "; #@! TF.CreationDate,2026-09-22T10:14:03+05:00", "; #@! TF.GenerationSoftware,Kicad,Pcbnew,8.0.4",
                 f"; #@! TF.FileFunction,{function}", "FMAT,2", "METRIC"]
        for t, d in tools:
            lines.append(f"T{t}C{d:.3f}")
        lines += ["%", "G90", "G05"]
        for t, pts in hits:
            lines.append(f"T{t}")
            for x, y in pts:
                lines.append(f"X{x:.3f}Y{y:.3f}")
        for t, (x1, y1, x2, y2) in slots:
            lines += [f"T{t}", f"G00X{x1:.3f}Y{y1:.3f}", "M15", f"G01X{x2:.3f}Y{y2:.3f}", "M16", "G05"]
        lines.append("M30")
        return "\n".join(lines) + "\n"

    j2 = [(46.0 + rot(dx, dy, 90)[0], 13.0 + rot(dx, dy, 90)[1]) for dx, dy in J2_PADS]
    pth = drill("Plated,1,2,PTH", [(1, 0.3), (2, 0.75), (3, 0.6)], [(1, VIAS), (2, j2)],
                slots=[(3, (3.2 + sy - h / 2 + 0.3, 16.0 + sx, 3.2 + sy + h / 2 - 0.3, 16.0 + sx))
                       for sx in (-4.32, 4.32) for sy, h in ((-1.15, 2.0), (2.85, 1.6))])
    npth = drill("NonPlated,1,2,NPTH", [(1, 3.2)], [(1, MOUNT)])

    job = {
        "Header": {"GenerationSoftware": {"Vendor": "KiCad", "Application": "Pcbnew", "Version": "8.0.4"},
                   "CreationDate": "2026-09-22T10:14:03+05:00"},
        "GeneralSpecs": {"ProjectId": {"Name": "pwr-board", "Revision": "B"}, "Size": {"X": W, "Y": H},
                         "LayerNumber": 2, "BoardThickness": 1.6, "Finish": "HAL SnPb"},
        "FilesAttributes": [],
    }
    files = {
        f"{stem}-F_Cu.gtl": f_cu.render(), f"{stem}-B_Cu.gbl": b_cu.render(),
        f"{stem}-F_Mask.gts": f_mask.render(), f"{stem}-B_Mask.gbs": b_mask.render(),
        f"{stem}-F_Silkscreen.gto": f_silk.render(), f"{stem}-B_Silkscreen.gbo": b_silk.render(),
        f"{stem}-F_Paste.gtp": f_paste.render(), f"{stem}-Edge_Cuts.gm1": edge.render(),
        f"{stem}-PTH.drl": pth, f"{stem}-NPTH.drl": npth, f"{stem}-job.gbrjob": json.dumps(job, indent=2),
    }
    return {k: v.encode() for k, v in files.items()}


def gerber_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in build_files().items():
            z.writestr(name, data)
    return buf.getvalue()


def pos_csv():
    """KiCad 'Component placement' CSV (File → Fabrication outputs → Component placement)."""
    rows = ["Ref,Val,Package,PosX,PosY,Rot,Side"]
    for ref, val, pkg, x, y, r, side, _pads in sorted(PARTS, key=lambda p: (p[0][0], int(p[0][1:]))):
        rows.append(f'"{ref}","{val}","{pkg}",{x:.4f},{y:.4f},{float(r):.4f},{side}')
    return ("\n".join(rows) + "\n").encode()

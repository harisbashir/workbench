"""The browser editor and the server exports must lay diagrams out identically."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from apps.diagrams import geometry, starters

HERE = Path(__file__).parent


def _norm(v):
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


def _sample():
    d = starters.board_high_level("Power board — with a long name that wraps onto two lines")
    d["nodes"] += [
        {"id": "x1", "type": "diamond", "x": 900, "y": 100, "w": 120, "h": 80, "label": "Mux", "sub": "TS3A5018"},
        {"id": "x2", "type": "text", "x": 900, "y": 250, "w": 140, "h": 30, "label": "Note: 10 kΩ pull-ups"},
        {"id": "x3", "type": "battery", "x": 1100, "y": 120, "w": 100, "h": 60, "label": "Li-ion", "color": "dark"},
        {"id": "x4", "type": "board", "x": 1100, "y": 260, "w": 180, "h": 100, "label": "Control board", "sub": "Rev A"},
    ]
    d["edges"] += [
        {"id": "y1", "from": "x1", "to": "n5", "kind": "analog", "label": "ADC\nch 3", "arrow": "start", "route": "straight"},
        {"id": "y2", "from": "x3", "to": "x4", "kind": "power", "label": "VBAT", "arrow": "both", "fromSide": "bottom", "toSide": "top"},
        {"id": "y3", "from": "x4", "to": "n5", "kind": "highspeed", "label": "USB", "arrow": "none"},
        {"id": "y4", "from": "x4", "to": "x1", "kind": "rf", "label": "", "arrow": "end"},
    ]
    return geometry.clean(d)


@skipUnless(shutil.which("node"), "Node.js not installed")
class ParityTests(SimpleTestCase):
    def test_same_layout_in_browser_and_server(self):
        data = _sample()
        py = [list(p[:5]) if p[0] == "label_bg" else list(p[:-1]) for p in geometry.layout(data)]
        with tempfile.TemporaryDirectory() as tmp:
            sp, dp = Path(tmp) / "s.json", Path(tmp) / "d.json"
            sp.write_text(json.dumps(geometry.STYLE))
            dp.write_text(json.dumps(data))
            out = subprocess.run(["node", str(HERE / "parity.js"), str(sp), str(dp)], capture_output=True, text=True, check=True)
        js = json.loads(out.stdout)
        self.assertEqual(len(js), len(py))
        for a, b in zip(_norm(py), _norm(js)):
            self.assertEqual(a, b)

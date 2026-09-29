// Usage: node parity.js style.json data.json  → prints the layout as JSON (used by test_parity.py)
const fs = require("fs");
global.window = global;
require(__dirname + "/../../../static/js/diagram_geometry.js");
const style = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
DiagramGeometry.init(style);
const out = DiagramGeometry.layout(data).prims.map((p) => p.slice(0, p[0] === "label_bg" ? 5 : p.length - 1));
process.stdout.write(JSON.stringify(out));

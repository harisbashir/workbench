# KiCad Previews (1.7.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In Sources, KiCad 6–9 schematics and PCBs open as drawings, not text: schematic sheets with hierarchy navigation and part details, the PCB in the existing board viewer (top, bottom, layers, solder-mask colours, measure), a readable change summary between versions (parts, values, footprints, nets, moved parts, tracks), and a visual compare that highlights what was added, removed or changed.

**Architecture:** Package `apps/sources/kicad/`:
- `sexpr.py`: a tokenizer and parser into nested lists, with size, depth and node limits.
- `schematic.py`: parse → `Schematic` model (symbols with their lib graphics, wires, junctions, labels, sheets, text) → SVG.
- `pcb.py`: parse → `Board` model (footprints with pads and graphics, tracks, arcs, vias, zones' filled polygons, board graphics) → `gerber.LayerData` objects → the shared SVG builder extracted from `design/board.py` (`assemble(...)`), so `static/js/pcb.js` works unchanged.
- `compare.py`: semantic diff of two parsed files plus highlight overlays.
- `render.py`: a disk cache under `DATA_DIR/cache/kicad/` keyed by blob SHA-256 and `RENDER_VERSION`, and `prerender` for the scheduler.

Views in `apps/sources/views.py` serve the HTML page and the sandboxed SVG (the same CSP as `design._svg_response`).

**Tech Stack:** pure Python. No new dependencies, no kicad-cli.

**Spec:** `docs/superpowers/specs/2026-10-19-sources-and-controllers-design.md` (Part 3).

## Global Constraints

- Supported files: `.kicad_sch` and `.kicad_pcb` with `(version N)` from 20211014 (KiCad 6) to 2026xxxx; anything newer still renders, with the note "Made with a newer KiCad; some details may be missing". Legacy `.sch` / `.kicad_pcb` files from KiCad 5 or older (`(kicad_pcb (version 2017…`, `EESchema Schematic File Version`) show "This KiCad version isn't supported for previews."
- Limits:
  - file size 50 MB
  - nesting depth 200
  - 5,000,000 parsed nodes
  - drawing shares `gerber.Budget` (time and element limits)
  - any limit error → a friendly message, never a 500
- SVG is served only from a dedicated view with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` and `nosniff`. All text from files is XML-escaped (`gerber.escape`).
- Pages follow the strict CSP: JS in `static/js/` (reuse `pcb.js`; new `kicad_sch.js` for sheet pan/zoom and part click), no inline handlers or styles.
- Permissions as in Sources: through the project; viewers can see previews.
- Cache: rebuildable, `DATA_DIR/cache/kicad/<sha>-<RENDER_VERSION>.json`. The scheduler pre-renders KiCad files from versions checked in within the last 7 days (at most 20 files per tick).
- Text: plain and friendly; the page says the preview is approximate.

## Review Focus

1. **Hostile s-expressions** (unbalanced parens, a 10,000-deep nest, a huge number, a giant string, a `NaN` coordinate) → a friendly error, with no recursion crash, no hang and no 500. Test: Task 1 `test_hostile_inputs`.
2. **Hierarchical sheets** that reference a missing file, a file outside the repository (`../x.kicad_sch`), or themselves (a loop) → shown as a sheet box with "not in this version" and no infinite recursion. Test: Task 3 `test_sheet_links_safe`.
3. **Text with markup characters** (`<script>`, `&`, `"`) in references, values, labels and titles → escaped in the SVG and the HTML. Test: Task 2 `test_text_escaped`.
4. **Rotated and mirrored symbols and footprints** (90/180/270, mirror x/y, bottom-side parts) → placed where KiCad shows them. Test: Task 2 `test_symbol_transform`, Task 4 `test_footprint_transform`.
5. **Compare where both versions have the same part with a different value or position, or a part renamed** → listed as changed, not as added plus removed. Test: Task 6 `test_value_change_is_change`.

---

### Task 1: S-expression parser (`kicad/sexpr.py`)

**Interfaces — Produces:**
- `class KicadError(ValueError)`: its message is shown to users.
- `parse(text: str) -> list` → nested lists. Atoms: bare words become `str`, quoted strings become `Str` (a `str` subclass), and numbers stay as their text. Callers use `num(x)`. Limits: `MAX_BYTES = 50 MB`, `MAX_DEPTH = 200`, `MAX_NODES = 5_000_000`. The parser is iterative, not recursive.
- `num(atom, default=0.0) -> float`: rejects NaN, inf and `abs > 1e6` (returns `default`).
- Helpers for any node `n`:
  - `head(n)`: the first atom.
  - `find(n, name)`: the first child list with that head.
  - `find_all(n, name)`.
  - `value(n, name, default=None)`: the second element of `find(n, name)`.
  - `xy(n, name="at") -> (x, y, angle)`.
  - `has_flag(n, flag)`: covers `hide` / `(hide yes)` style flags across versions.
- `file_version(tree) -> int`.

- [ ] Failing tests `test_kicad_sexpr.py`:
  - `test_parses_nested_and_strings`: `'(a (b "x y" 1.5) c)'` → `["a", ["b", "x y", "1.5"], "c"]`, with escapes `\"` and `\\`.
  - `test_helpers`.
  - `test_hostile_inputs`: unbalanced input, depth 10,000, a 60 MB string (patch MAX_BYTES), NaN coordinates via `num`, and an unterminated string all raise `KicadError` or return safe defaults.
- [ ] Implement → green → commit "KiCad previews: a safe s-expression parser".

### Task 2: Schematic model and drawing (`kicad/schematic.py`)

**Interfaces — Produces:**
- `load_schematic(text) -> Schematic`, with fields:
  - `version`, `title`, `paper` (w, h mm)
  - `symbols: [Symbol(ref, value, footprint, lib_id, at, angle, mirror, unit, fields: dict, uuid, dnp, power)]`
  - `wires`, `buses` (point lists), `junctions`, `no_connects`
  - `labels: [(kind local/global/hierarchical/power, text, x, y, angle)]`
  - `texts`
  - `sheets: [Sheet(name, file, x, y, w, h, pins)]`
  - `lib: {lib_id: LibSymbol(units: {(unit, style): [graphics]}, pins, power, pin_names_offset, hide_pin_numbers)}`
  - `nets() -> set[str]`: label texts and power values.
  - It reads property names "Reference", "Value", "Footprint", plus "Sheetname"/"Sheet name" and "Sheetfile"/"Sheet file" (the KiCad 6 names).
- `draw_schematic(sch, highlight: dict[str, str] | None = None, budget=None) -> str`: an SVG in mm, Y down, `viewBox` = page.
  - It draws the page frame, title block text, lib graphics (rectangle, polyline, circle, arc by start/mid/end, bezier, text, pins with their name and number), wires, buses, junction dots, no-connect crosses, labels (globals drawn as flag outlines), sheet boxes with name, file and pins, and field text.
  - The transform for lib points: `y = -y`, then rotate by the symbol angle (counter-clockwise on screen), then mirror (`mirror x` flips y, `mirror y` flips x), then translate.
  - Each symbol group is `<g data-ref="R1" data-uuid="…" class="sym">`. `highlight` maps a uuid to `added`, `removed` or `changed`, and the colours are written as attributes.
  - Sheet groups are `<g data-sheet="buck.kicad_sch">`.
  - Colours come from a light palette: KiCad's default schematic colours.

- [ ] Failing tests `test_kicad_schematic.py`, with fixtures written by hand under `apps/sources/tests/fixtures/kicad/`:
  - `v6_simple.kicad_sch`: version 20211123, properties with `(id N)`, root `symbol_instances`.
  - `v8_hier.kicad_sch`: version 20231120, a root sheet with a `Buck` sheet, a global label, a power symbol, a rotated R and a mirrored C.
  - `v8_buck.kicad_sch`: the child sheet.
  - `v9_simple.kicad_sch`: version 20250114, `(hide yes)`, `embedded_fonts`.

  Tests:
  - `test_loads_v6_v8_v9`: refs, values and footprints are read in every version.
  - `test_symbol_transform`: a resistor's pin-1 end point at 0, 90, 180 and 270 degrees and mirrored, against hand-computed coordinates.
  - `test_draw_contains_parts_and_wires`: `data-ref="R1"`, wires and the junction circle are present.
  - `test_text_escaped`: a value `<b>&"` is escaped.
  - `test_legacy_refused`: `EESchema Schematic File Version 4` raises `KicadError("This KiCad version isn't supported for previews.")`.
- [ ] Implement → green → commit "KiCad previews: read and draw schematics".

### Task 3: Schematic page in Sources

**Interfaces — Produces:**
- `kicad/render.py`:
  - `cached(blob, kind) -> dict | None` and `render(blob, kind, extra="") -> dict`, which writes the cache.
  - The schematic result is `{"ok", "svg", "error", "info": {"title", "parts", "sheets": [...], "nets"}, "parts": [{ref, value, footprint, uuid}]}`.
- URLs:
  - `…/<slug>/kicad/<int:v>/<path:path>`: the HTML page (`kicad_view`). The file viewer redirects there for `.kicad_sch`/`.kicad_pcb` unless `?text=1`.
  - `…/<slug>/kicad-svg/<int:v>/<path:path>`: the sandboxed SVG (`kicad_svg`), with `?compare=<n>` for overlays.
- Page:
  - breadcrumb "Root ▸ Buck", built from the sheet path (`?sheet=a.kicad_sch/b.kicad_sch`)
  - the SVG in an `<object>`-free fetch-and-inline viewer, using `static/js/kicad_sch.js` (fetch SVG text, insert, pan/zoom like pcb.js, click `g.sym` → fill in the side panel from `data-*`)
  - a sheet list, and a parts table (ref, value, footprint)
  - links: "View as text", "Download", "Compare with v{n-1}"
- Sheet links resolve inside the same version only: `posixpath.normpath(join(dirname(path), file))` must not start with `..`. A missing file shows "not in this version". A loop is stopped by the visited set (max depth 20).

- [ ] Failing tests `test_kicad_views.py`:
  - `test_schematic_page`: 200, parts table contains R1, link to the Buck sheet.
  - `test_svg_view_sandboxed`: the CSP header contains `sandbox`, the content type is `image/svg+xml`.
  - `test_sheet_links_safe`: `../evil.kicad_sch` and a self-reference both give 200 with the "not in this version" text.
  - `test_broken_file_message`: garbage `.kicad_sch` → 200 with the friendly error.
  - `test_permissions`: outsider 404/403.
  - `test_text_view_still_available`: `?text=1` shows highlighted text.
- [ ] Implement → green → commit "KiCad previews: schematic sheets in Sources".

### Task 4: PCB model and drawing (`kicad/pcb.py`) via the shared board builder

**Files:** Modify `apps/design/board.py`: extract `assemble(layers, shape, frame, warnings, job, files, prefix) -> dict` from `render_board` (from "def pick" to the return). `render_board` calls it. The existing design tests must stay green.

**Interfaces — Produces:**
- `load_pcb(text) -> Board`, with fields:
  - `version`, `thickness`, `layers: {name: type}`, `nets: {code: name}`
  - `footprints: [Footprint(ref, value, lib_id, x, y, angle, side, uuid, pads, graphics)]`
  - `tracks: [(layer, x1, y1, x2, y2, w, net)]`, `arcs`, `vias: [(x, y, size, drill, net)]`
  - `zones: [(layer, net, [polygons])]`, filled polygons only
  - `graphics: [(layer, kind, data, width, fill)]`
  - The footprint local transform rotates counter-clockwise on screen in Y-down coordinates, then translates. Pad `at` angles are absolute.
- `pcb_layers(board, budget) -> (layers: [LayerData], shape, frame, warnings)`. Kinds and sides:
  - F.Cu/B.Cu/In*.Cu → copper top/bottom/inner
  - *.Mask → mask, *.Paste → paste, *.SilkS → silk
  - Edge.Cuts → outline, which also gives the board shape as a path; an even-odd fill of closed loops is fine
  - holes (pad drills and vias) → one drill layer
  - Fab and Courtyard → kind "other", hidden by default in the layer list

  Pads: circle, rect, oval and roundrect are drawn exactly; trapezoid and custom are drawn as their bounding polygon; drill holes become circles or slots. Y is negated so the output matches Gerber orientation.
- `render_pcb(text, prefix="k") -> dict`: the same keys as `render_board`, plus `"parts": [...]`.

- [ ] Failing tests `test_kicad_pcb.py`, with fixtures `v6_board.kicad_pcb` (20211014, `fp_text reference`), `v8_board.kicad_pcb` (20240108, `property "Reference"`, a bottom-side part, a rotated part, a via, a zone with filled_polygon, an arc track) and `v9_board.kicad_pcb` (20241229, renumbered layers):
  - `test_loads_versions`
  - `test_footprint_transform`: the absolute position of pad 1 of a part rotated 90° at (10, 5).
  - `test_render_has_views_and_layers`: data-view top, bottom and layers; layer labels include "Top copper" and "Board outline".
  - `test_board_size_from_edge_cuts`
  - `test_design_render_unchanged`: run the existing `apps.design` tests.
- [ ] Implement (refactor first, with `apps.design` tests green) → green → commit "KiCad previews: draw PCBs in the board viewer".

### Task 5: PCB page in Sources

- The `kicad_view` page for `.kicad_pcb` reuses the `design/pcb_viewer.html` structure. Extract its toolbar, canvas and facts into an include, `templates/design/_pcb_stage.html`, which both pages use. `data-src` points at `kicad_svg`.
- The side panel: board facts (size, copper layers, thickness, parts on top/bottom, vias, smallest track), the layer list, and a parts table.

- [ ] Failing tests:
  - `test_pcb_page`: 200, has `data-pcb-canvas`, layer toggles and the parts table.
  - `test_pcb_svg`.
  - `test_design_viewer_still_works`: the existing design viewer test.
- [ ] Implement → green → commit "KiCad previews: PCB view in Sources".

### Task 6: Change summary and visual compare (`kicad/compare.py`)

**Interfaces — Produces:**
- `compare_schematics(a: Schematic, b: Schematic) -> dict`:
  - `added`, `removed`, `changed: [{ref, what: ["value 10k → 12k", "footprint …"]}]`
  - `nets_added`, `nets_removed`
  - `highlight_a`, `highlight_b` (uuid → state)

  Parts match by uuid first, then by reference.
- `compare_pcbs(a: Board, b: Board) -> dict`:
  - the same part lists, with `moved` and `rotated` and `side changed` as changes, using a 0.01 mm tolerance
  - nets added and removed
  - `tracks: (count a, count b, length a, length b)`, `vias: (a, b)`
  - highlight dicts by uuid

  PCB overlays draw a translucent box around each highlighted footprint's courtyard or pads bounding box on a top-level `<g data-view>`-independent group.
- URL `…/<slug>/kicad-compare/<int:a>/<int:b>/<path:path>` → `kicad_compare`: a summary table, plus side-by-side old/new SVGs with highlights (`kicad_svg?compare=<other>`) and a "show only changes" toggle (`kicad_sch.js` dims non-highlighted groups by toggling a class on the root).
- The Sources compare page shows a "Visual compare" link for changed KiCad files. The KiCad view page shows "Compare with v{n-1}".

- [ ] Failing tests `test_kicad_compare.py`:
  - `test_value_change_is_change` (and a renamed reference with the same uuid also counts as a change)
  - `test_added_removed_parts`
  - `test_nets_diff`
  - `test_pcb_moved_part`
  - `test_compare_page` (200, contains "10k → 12k")
- [ ] Implement → green → commit "KiCad previews: change summary and visual compare".

### Task 7: Pre-render, cache housekeeping, demo, help, README, release 1.7.0

- `render.prerender(limit=20)` is called from scheduler `tick()` as `step("kicad previews", …)`. The daily cleanup removes cache files older than 30 days.
- `seed_demo`: replace the demo KiCad repository's placeholder files with the v8 fixtures (a 2-sheet schematic and a small board), and add a second version that changes R7's value and moves a part, so the visual compare shows something.
- `templates/core/help/sources.html`: a section on KiCad previews and their limits.
- README: an entry under Features, plus "Upgrading to 1.7".
- `VERSION` 1.7.0.
- Tests:
  - `test_prerender_fills_cache`
  - `test_cache_cleanup`
  - a seed run (existing `seed_demo` test if present, else a smoke run in verification)
- [ ] Full suite, ruff, migration check, deploy check, seed + page smoke run (every KiCad page on the demo) → final review → merge, push, CI green, tag `v1.7.0`, sync Windows.

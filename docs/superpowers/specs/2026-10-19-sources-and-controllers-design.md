# Sources, controllers and KiCad previews — design

Date: 2026-10-19 · Status: approved in conversation, awaiting spec review

## Why

Workbench handles the *released* side of hardware well (revisions, final Gerbers, firmware
releases, builds) but has nowhere for work in progress, and it can't say which firmware runs
on which chip of a board. Teams currently depend on GitHub for code and on shared drives for
KiCad projects. The goal is to make Workbench a self-contained tool for hardware *and* firmware
development, with only basic, simple GitHub-like features, without depending on GitHub.

## Decisions made

| Question | Decision |
|---|---|
| Where code and design files live | Inside Workbench, in a new **Sources** section. GitHub use is minimised (the existing webhook link stays as-is, optional). |
| How people save | **Browser check-in** (files, folder or zip + message). No git clone/push in this round. |
| Storage | Snapshot versions over **content-addressed blobs** in the existing storage API (local disk or S3, unchanged). No git on disk, so S3 production servers need nothing new. |
| Concurrent edits | **KiCad schematic/PCB files must be checked out (locked)** to change them; all other files use a **stale check** against the version you started from. |
| Code preview | Server-side syntax highlighting (Pygments), line links, highlighted diffs. |
| KiCad preview | **Built-in renderer** for KiCad 6–9 `.kicad_sch` / `.kicad_pcb` → SVG. No kicad-cli in the image. Close to KiCad, not pixel-identical. |
| Firmware on boards | **Controllers** on each board; a **firmware set** per revision pins the released version each controller ships with. |

## Delivery

Three releases, each usable on its own, each with its own implementation plan:

1. **1.5.0 — Controllers & firmware sets** (builds on existing models)
2. **1.6.0 — Sources** (repositories, check-in, history, diffs, code viewer, locks)
3. **1.7.0 — KiCad previews** (parser, schematic + PCB views, change summary, visual compare)

---

## Part 1 — Controllers & firmware sets (1.5.0)

### Model (in `apps/projects` for Controller, `apps/firmware` for the rest)

- **Controller** — belongs to a `Board`. Fields: `designator` (e.g. U1, unique per board),
  `name` (e.g. "Main MCU"), `part` (e.g. STM32G474RE), `notes`, `order`.
- **ControllerSlot** — a firmware that runs on a controller: `controller`, `firmware`
  (FK `firmware.Firmware`, same project), `flash_order` (bootloader before application).
  Unique `(controller, firmware)`. A controller can have several slots.
- **FirmwareSetEntry** — what a revision ships with: `revision`, `slot`, `release`
  (FK `FirmwareRelease`, must belong to `slot.firmware`). Unique `(revision, slot)`.
- **Revision.firmware_set_locked** (bool) — set by a lead/manager when the revision is
  released; locked sets can't change except by unlocking (audited).
- **BuildFirmware** — a build's copy of the set: `build`, `controller_label` (text snapshot,
  e.g. "U1 Main MCU"), `slot` (nullable FK, SET_NULL), `release` (PROTECT), `flash_order`.
  Copied from the revision when the build is created; edits to the revision later don't change it.

Migration: existing `BuildOrder.firmware_releases` links become `BuildFirmware` rows with an
empty slot and `controller_label = ""`; then the old M2M field is removed. Firmware with a
release that is linked to a build stays PROTECTed as before.

### Rules

- Default choice for a slot: `firmware.recommended_for(revision)` (newest released compatible).
- Revisions with status *Released for production* accept only **Released** firmware versions.
- Warnings shown on the revision, build and firmware release pages: empty slot, release recalled or
  deprecated, release not marked compatible with the revision. Recalling a release surfaces
  the warning everywhere it is used.
- All changes go through permission checks (`project.can_edit`), are atomic, and audited:
  `controller.created/updated/deleted`, `revision.firmware_set`, `revision.firmware_set_lock`.

### Flashing bundle

- Download per revision and per build: a zip of every image artifact (kind Image/Bootloader/
  Package) of the chosen releases plus `manifest.json`:
  `[{controller, designator, firmware, version, file, sha256, flash_order}]`.
- Built in the request via the storage API with a total size cap (`MAX_UPLOAD_MB`); served
  through `apps/core/storage` permission-checked responses. Browser download only in this
  round (no API tokens exist yet; tokens for flashing stations are a later follow-up).

### UI

- Board page: "Controllers" panel (add/edit/remove, slots per controller).
- Revision page: "Firmware" panel — table of controller × slot → version picker, warnings,
  lock toggle, "Download flashing bundle".
- Build page: the copied firmware list + bundle download (replaces "Firmware to flash").
- Firmware page: "Used on" — boards/controllers and revisions.

---

## Part 2 — Sources (1.6.0)

New Django app `apps/sources`, URL `/sources/`, templates `templates/sources/`.
A "Sources" item in the main navigation and a Sources tab on each project.

### Structure

Project → shared repositories; Board → its repositories. Same permission model as projects:
viewers browse/download, writers check in and lock, managers/admins break locks.

### Model

- **Repository** — `project`, `board` (nullable = project-wide), `name`, `slug`
  (unique per project), `kind` (firmware / kicad / other), `controller` (nullable FK),
  `firmware` (nullable FK — links versions to firmware releases), `description`,
  `latest_number`, `created_by/at`.
- **Blob** — `sha256` (unique), `size`, `file` (storage path `sources/blobs/ab/cd/<sha256>`),
  `is_text` (detected on store), `created_at`. Contents are stored once, shared across repos.
- **Version** — `repository`, `number` (1, 2, …, unique per repo), `message`, `author`,
  `created_at`, `parent` (previous version), counts `added/changed/deleted`,
  `lines_added/lines_removed`.
- **VersionFile** — `version`, `path` (normalised, `/`-separated, max 400), `blob`.
  Every version lists its full tree (unchanged files point at the same blob; cheap at
  hardware-team scale). Unique `(version, path)`.
- **Lock** — `repository`, `path`, `user`, `created_at`. Unique `(repository, path)`.

### Check-in

1. Upload files, a folder (`webkitdirectory`), or a zip; choose **Replace the whole
   repository** (missing files are deleted) or **Add/update these files only**; write a message;
   the form carries `base` = the version number the user started from (the one they downloaded
   or are looking at).
2. Uploaded content is staged to blobs; Workbench shows a **preview**: new / changed / deleted /
   unchanged, plus refusals. Confirm commits it.
3. Commit, in one transaction with `select_for_update` on the repository:
   - **Stale check**: any file the upload changes or deletes that also changed between `base`
     and the latest version is refused, with a link to the diff. If any file is refused, nothing
     is committed by default; the preview offers "Check in the other files only", which commits
     the rest. The user then downloads the latest version, merges, and checks in again.
   - **Locks**: changing or deleting `.kicad_sch` / `.kicad_pcb` requires the user to hold that
     file's lock. Locks are optional for other files but honoured if someone holds one.
   - Number = `latest_number + 1`; write Version + VersionFiles; update counters. Empty
     check-ins (nothing changed) are refused.
   - Option "release my locks after check-in" (default on).
4. Audit: `sources.checkin`, `sources.lock`, `sources.unlock`, `sources.lock_broken`.
   Project activity line: "Bilal checked in Main MCU firmware v13 — Add CAN driver".

### Limits (untrusted uploads)

- Total per check-in follows `settings.MAX_UPLOAD_MB`; max 5,000 files per check-in; max
  path length 400; zip handling reuses the patterns in `apps/design/board.py` (entry count,
  total uncompressed size, compression-ratio check, no absolute paths or `..`, no symlinks).
- `BLOCKED_EXTENSIONS` from `apps/files/models.py` apply.
- Ignored on upload: `.git/`, `__pycache__/`, KiCad backups (`*-backups/`, `*.kicad_prl`,
  `fp-info-cache`, `_autosave-*`), `.DS_Store`, `Thumbs.db`.

### Browsing and previews

- Repository page: file tree of the latest version (or `?v=N`), history list, locks list.
- File viewer: text/code highlighted with **Pygments** (`HtmlFormatter(nowrap/classes)`, styles
  in `app.css`, light and dark), line numbers, `#L42` anchors. Max 1 MB / 20,000 lines highlighted;
  above that plain text; binary shows size + download. PNG/JPG shown inline from a
  permission-checked view; SVG/HTML/PDF are download-only. `README.md` rendered via the existing
  `apps/chat/formatting.render` style (escaped, no raw HTML).
- KiCad files open in the KiCad viewer (Part 3); until 1.7.0 they show as text.
- **Compare** any two versions (default: a version vs its parent): list of added/changed/deleted
  files, each with unified or side-by-side diff (`difflib`, highlighted), unchanged runs folded.
  Per-file limits: 1 MB each side, 2 s; above that "too large to compare" plus both downloads.
- Download any version as zip (streamed, `force_zip64`), or a single file.

### Promote to production

- Firmware repo version → **Draft firmware release** of the linked firmware: version number
  suggested by `suggest_next_version()`, `git_ref = "sources v14"`, source zip attached as an
  Other artifact, link back to the version. Binaries are then uploaded as today.
- KiCad repo version → **link to a board revision** (`Revision.source_version`, nullable FK):
  "Built from KiCad design v22". Final Gerbers stay in the revision's design files.

### Background jobs (scheduler)

- Garbage-collect blobs not referenced by any VersionFile and older than 1 day (staged but never
  committed, or from deleted repositories).
- Export: Sources is added to `apps/core/export.build_export` (latest version of each repo as
  files, plus a history CSV). Backups already include all stored files.

---

## Part 3 — KiCad previews (1.7.0)

Module `apps/sources/kicad/`: `sexpr.py` (tokeniser/parser), `schematic.py`, `pcb.py`,
`render.py` (cache), `compare.py`.

### Parser

- S-expression parser for KiCad 6–9 (`kicad_sch` versions 2021xxxx–2025xxxx,
  `kicad_pcb` 2021xxxx–2025xxxx). Unknown tokens are ignored, so newer files degrade
  gracefully; files from KiCad 5 (legacy formats) show "this KiCad version isn't supported
  for previews".
- Limits like `apps/design/gerber.py`: max file size 50 MB, max nesting depth, max elements
  per render, max coordinates, time limit per render.

### Schematic view

- Draws each sheet to SVG: symbols from the file's embedded `lib_symbols` (rectangles,
  polylines, arcs, circles, pins, units/De Morgan body style), wires, buses, junctions,
  no-connects, labels (local/global/hierarchical), power symbols, text, sheet frames.
  Fields (reference, value) shown. Text uses an SVG font stack approximating KiCad's stroke font.
- Hierarchy: sheet symbols are clickable and open the child `.kicad_sch` from the same version;
  breadcrumb "Root ▸ Power ▸ Buck".
- Click a symbol → its reference, value, footprint, fields.

### PCB view

- Converts the board to per-layer SVG and shows it in the existing `static/js/pcb.js` layer
  viewer (layer toggles, pan/zoom, top/bottom): edge cuts, copper (tracks, arcs, vias, pads by
  shape, filled zones from the file's `filled_polygon` data), silkscreen, fab, courtyard, mask.
  Footprint graphics and text included. Board size and layer count shown.

### Change summary and visual compare

- Between two versions of a schematic/PCB: components added/removed, value/footprint changed,
  moved (PCB), nets added/removed/renamed, track/via count delta.
- Visual compare: one SVG with added (green), removed (red) and changed (amber) items
  highlighted, toggle "show only changes".

### Rendering pipeline

- Rendered by the scheduler after each check-in for changed KiCad files; cached on disk under
  `DATA_DIR/cache/kicad/<sha256>-<render version>` (rebuildable, like Gerber renders).
- The page shows "Preparing preview…" until the cache entry exists, and falls back to rendering
  on request within the time limit if the scheduler hasn't caught up.

---

## Cross-cutting

- Strict CSP: all JS in `static/js/` (new `sources.js`, `kicad_view.js` reusing `pcb.js`),
  styling in `app.css`; no inline scripts/styles/handlers.
- Every object is looked up through its project; ids from other projects 404.
- Links/buttons hidden for actions the user can't perform.
- New dependency: `Pygments>=2.18,<3`.
- Help guides: new `templates/core/help/sources.html`; update `firmware.html`, `hardware.html`,
  `production.html`. README: notable changes per release.
- Demo data (`seed_demo`): controllers + firmware sets on demo boards; a demo firmware
  repository with a few versions; a small KiCad project (1.7.0).

## Testing

Each release adds tests in the app's `tests/` folder:

- Models and atomic operations (concurrent check-in numbering, lock races, double submits).
- Permissions: viewers can't write, other-project ids 404, lock breaking needs manager.
- Hostile uploads: zip bombs, path traversal, blocked extensions, oversize, too many files.
- Stale check and lock enforcement; replace-all vs update-only semantics.
- Diff and highlight limits.
- Migration of existing `BuildOrder.firmware_releases` data.
- KiCad parser/renderer on sample files saved by KiCad 6, 7, 8 and 9 (fixtures committed),
  plus malformed/hostile files hitting limits.
- CI gates stay green: ruff, shellcheck, tests, `makemigrations --check`, `check --deploy`,
  Docker build, container health check.

## Out of scope (this round)

- git clone/push access, branches, merge requests.
- Two-way GitHub sync.
- API tokens for flashing stations.
- Server-side Gerber generation from KiCad (no kicad-cli).
- Per-unit serial registry and change requests (ECOs) — good follow-ups.

## Release workflow

Develop in `/home/haris/development/github/workbench`, keep CI green, commit, tag, push to
`github.com/harisbashir/workbench`, then `git pull` in
`/mnt/hgfs/GitHub/project-workbench/workbench` (one-time local git config there:
`safe.directory`, `core.fileMode=false`). Production updates with `./update.sh`.

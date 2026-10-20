# Controllers & Firmware Sets (1.5.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boards list their controllers (MCUs) and the firmware each runs; each revision pins the released version per controller; builds copy that set; a flashing bundle zips the images with a manifest.

**Architecture:** `Controller` lives in `apps/projects` (it is part of a board). `ControllerSlot` and `FirmwareSetEntry` live in `apps/firmware`, with all rules in one module `apps/firmware/sets.py` that views call. Builds get a `BuildFirmware` table (a snapshot) that replaces the `BuildOrder.firmware_releases` many-to-many; existing links are migrated. Controller and set views live in `apps/firmware` so `apps/projects` doesn't import firmware.

**Tech Stack:** Django 5.2, server-rendered templates, `static/js/app.js` only if a control needs JS (none planned), Django `TestCase`.

**Spec:** `docs/superpowers/specs/2026-10-19-sources-and-controllers-design.md` (Part 1).

## Global Constraints

- Strict CSP: no inline `<script>`, no `style="…"`, no `on*=` handlers. Styling via classes in `static/css/app.css`.
- Every view checks permissions on the server; objects are looked up through their project (`get_object_or_404(..., project=project)`), so ids from other projects 404. Viewers are read-only; buttons hidden for actions the user can't do.
- Writers (`project.can_edit(user)`) manage controllers and the firmware set; only `user.can_manage_projects` (lead/admin) locks or unlocks a set.
- Set and build-firmware changes are atomic (`transaction.atomic()` + `select_for_update()` on the revision/build row).
- Audit actions: `controller.created`, `controller.updated`, `controller.deleted`, `revision.firmware_set`, `revision.firmware_set_lock`, `revision.firmware_set_unlock`, `build.firmware`.
- A revision with status `released` ("Released for production") accepts only `released` firmware versions in its set. Draft versions are never selectable anywhere. Builds accept `released` or `testing` (today's rule).
- Downloads use `apps.core.storage.file_response`-style permission-checked responses. Bundle total size cap: `settings.MAX_UPLOAD_MB` MB.
- UI text: plain, friendly English, short sentences.
- Every task adds tests under `apps/<app>/tests/`; `python manage.py makemigrations --check --dry-run` and `ruff check apps config --select F,E9` stay clean.

## Review Focus

1. Firmware or release from **another project** posted into a set or build (crafted form) → refused, nothing saved. Test in Task 2 (`test_save_set_refuses_other_project_release`) and Task 5 (`test_set_build_firmware_refuses_other_project`).
2. **Recalled** release after it was pinned → revision, build and bundle all show it; build can't start; bundle refuses. Tests in Task 2 (`test_issues_recalled`), Task 5 (`test_start_blocked_by_recalled_entry`), Task 6 (`test_bundle_refuses_recalled`).
3. **Deleting a controller or firmware** that a set/build references → set entries go (CASCADE), build snapshot rows keep their release and label (slot SET_NULL); a release used by a build can't be deleted (PROTECT). Test in Task 5 (`test_build_snapshot_survives_controller_delete`).
4. **Two people saving the set at once / double submit** → one consistent result, no duplicate entries (unique constraint + `update_or_create` under the row lock). Test in Task 2 (`test_save_set_twice_is_idempotent`).
5. **Existing production data**: builds with firmware linked before upgrade keep the same releases after migration. Test in Task 5 (`test_migration_moves_build_firmware`).

---

### Task 1: Models and migrations (controllers, slots, set entries, lock flag)

**Files:**
- Modify: `apps/projects/models.py` (add `Controller` after `Board`; add `Revision.firmware_set_locked`)
- Modify: `apps/firmware/models.py` (add `ControllerSlot`, `FirmwareSetEntry`)
- Create: `apps/projects/migrations/0006_controller_firmware_set_locked.py`, `apps/firmware/migrations/0003_controllerslot_firmwaresetentry.py` (via `makemigrations`)
- Test: `apps/firmware/tests/test_sets.py` (new file; model tests now, logic tests in Task 2)

**Interfaces:**
- Produces:
  - `projects.Controller(board FK Board CASCADE related_name="controllers", designator CharField(20), name CharField(80), part CharField(80, blank), notes CharField(200, blank), order PositiveIntegerField(default=0))`; Meta `ordering=["board", "order", "designator"]`, `unique_together=[("board", "designator")]`; `__str__` → `"U1 Main MCU"`; property `project` → `self.board.project`.
  - `Revision.firmware_set_locked = BooleanField(default=False)`.
  - `firmware.ControllerSlot(controller FK projects.Controller CASCADE related_name="slots", firmware FK Firmware CASCADE related_name="slots", flash_order PositiveIntegerField(default=1))`; Meta `ordering=["controller__order", "controller__designator", "flash_order", "firmware__name"]`, `unique_together=[("controller", "firmware")]`; `clean()` raises `ValidationError` if `firmware.project_id != controller.board.project_id`.
  - `firmware.FirmwareSetEntry(revision FK projects.Revision CASCADE related_name="firmware_set", slot FK ControllerSlot CASCADE related_name="entries", release FK FirmwareRelease PROTECT related_name="set_entries", updated_by FK user SET_NULL null, updated_at DateTimeField(auto_now))`; `unique_together=[("revision", "slot")]`.

- [ ] **Step 1: Write failing tests** in `apps/firmware/tests/test_sets.py`, class `ModelTests` (setUp: project `PWR`, board from `default_board`, firmware "Main application", one released release):
  - `test_controller_str_and_unique_designator`: `str(c) == "U1 Main MCU"`; creating a second `U1` on the same board raises `IntegrityError` (wrap in `transaction.atomic()`).
  - `test_slot_rejects_other_project_firmware`: slot with firmware from project `OTH` → `slot.full_clean()` raises `ValidationError`.
  - `test_entry_unique_per_revision_slot`: second `FirmwareSetEntry` for same (revision, slot) raises `IntegrityError`.
  - `test_release_in_set_is_protected`: deleting a release referenced by an entry raises `ProtectedError`.
- [ ] **Step 2: Run** `python manage.py test apps.firmware.tests.test_sets` → FAIL (import errors).
- [ ] **Step 3: Add the models and run** `python manage.py makemigrations projects firmware` (names above). Migrations are additive only.
- [ ] **Step 4: Run** `python manage.py test apps.firmware.tests.test_sets` → PASS; `python manage.py makemigrations --check --dry-run` → "No changes detected".
- [ ] **Step 5: Commit** `git commit -m "Add board controllers, firmware slots and revision firmware sets (models)"`.

### Task 2: Firmware set rules (`apps/firmware/sets.py`)

**Files:**
- Create: `apps/firmware/sets.py`
- Test: `apps/firmware/tests/test_sets.py` (class `SetRuleTests`)

**Interfaces:**
- Consumes: Task 1 models; `Firmware.recommended_for(revision)`.
- Produces:
  - `issues_for(release, revision) -> list[tuple[str, str]]` — `("bad", "Recalled — do not flash")` if recalled; `("warn", "Deprecated — replaced by a newer version")` if deprecated; `("warn", "<Status> — not released yet")` if testing; `("warn", f"Not marked compatible with {revision.title}")` if revision not in `release.revisions`; `("warn", f"Newer release {v} is available")` if `firmware.recommended_for(revision)` sorts higher. Same wording as the current build page (moved here so build and revision share it).
  - `slots_for(revision) -> QuerySet[ControllerSlot]` — slots of controllers on `revision.board`, `select_related("controller", "firmware")`.
  - `set_rows(revision) -> list[dict]` — one dict per slot: `{"slot", "entry" (or None), "release" (entry's or None), "recommended", "choices" (selectable releases), "issues"}`; an empty slot gets `issues=[("warn", "No version chosen")]`.
  - `selectable_releases(slot, revision) -> QuerySet[FirmwareRelease]` — releases of `slot.firmware`, status `released` only if `revision.status == "released"`, else `released` or `testing`; ordered `-sort_key`.
  - `save_set(revision, choices: dict[int, int | None], user) -> list[str]` — `choices` maps slot id → release id (None clears). In `transaction.atomic()`, re-reads the revision with `select_for_update()`; returns `["This firmware set is locked. Ask a lead to unlock it."]` without changes if locked; ignores slot ids not in `slots_for(revision)`; for each release id not in `selectable_releases(slot, revision)` adds an error `f"{slot.firmware.name} {version or '?'} can't be used on {revision.title}."` and saves nothing for that slot; otherwise `update_or_create` / delete. Returns the error list (empty = all saved).
  - `set_lock(revision, locked: bool) -> bool` — conditional `Revision.objects.filter(pk=..., firmware_set_locked=not locked).update(firmware_set_locked=locked)`; returns whether it changed.
  - `missing_count(revision) -> int` — slots without an entry (used for the "N controllers have no firmware chosen" warning).

- [ ] **Step 1: Write failing tests** in `SetRuleTests` (setUp: project PWR, board, controller U1 with slots for "Bootloader" (flash_order 1) and "Main application" (flash_order 2); releases app 1.0.0 released + compatible with rev, app 1.1.0 testing, app 0.9.0 recalled, boot 1.0.0 released; second project OTH with its own released release):
  - `test_issues_recalled`: `("bad", "Recalled — do not flash") in issues_for(app_090, rev)`.
  - `test_issues_not_compatible_and_newer`: release not linked to rev → contains `("warn", "Not marked compatible with …")`; pinned 1.0.0 while 1.2.0 released+compatible → contains `("warn", "Newer release 1.2.0 is available")`.
  - `test_set_rows_marks_empty_slot`: no entries → both rows have `("warn", "No version chosen")`, rows ordered boot then app.
  - `test_save_set_saves_and_clears`: save `{boot: boot100, app: app100}` → 2 entries; save `{app: None}` → app entry deleted, boot kept.
  - `test_released_revision_only_accepts_released`: `rev.status = "released"`; saving `app 1.1.0` (testing) returns one error, no entry created.
  - `test_save_set_refuses_other_project_release`: saving OTH's release id for the app slot → error, nothing saved.
  - `test_locked_set_refuses_changes`: `set_lock(rev, True)` is True, second call False; `save_set` returns the locked message and changes nothing.
  - `test_save_set_twice_is_idempotent`: same choices saved twice → still exactly 2 entries.
- [ ] **Step 2: Run** `python manage.py test apps.firmware.tests.test_sets` → new tests FAIL.
- [ ] **Step 3: Implement `apps/firmware/sets.py`** with the signatures above.
- [ ] **Step 4: Run** the tests → PASS.
- [ ] **Step 5: Commit** `git commit -m "Firmware set rules: issues, selectable versions, atomic save and lock"`.

### Task 3: Controllers on the board page

**Files:**
- Create: `apps/firmware/board_views.py` (views), `templates/firmware/controller_form.html`, `templates/firmware/_controllers_card.html`
- Modify: `apps/firmware/urls.py`, `apps/projects/views.py:274-298` (`board_detail` adds `controllers` to context), `templates/projects/board_detail.html` (include the card, replacing the existing Firmware card at line 66), `static/css/app.css` (only if a new class is needed)
- Test: `apps/firmware/tests/test_controllers.py`

**Interfaces:**
- Consumes: Task 1 models; `apps.firmware.views._project(request, key, edit=False)`; `audit`, `log_activity`.
- Produces URLs (in `apps/firmware/urls.py`):
  - `projects/<key>/boards/<int:board>/controllers/new/` → `controller_edit` name `firmware:controller_create`
  - `projects/<key>/boards/<int:board>/controllers/<int:pk>/edit/` → `controller_edit` name `firmware:controller_edit`
  - `projects/<key>/boards/<int:board>/controllers/<int:pk>/delete/` (POST) → `controller_delete` name `firmware:controller_delete`
- `ControllerForm(ModelForm)` fields `designator, name, part, notes, order` plus `firmwares` (`ModelMultipleChoiceField` over `project.firmwares`, checkbox widget) and per-chosen-firmware flash order is the firmware's position in a bootloader-first ordering: bootloaders (name contains "boot", case-insensitive) get 1, others 2. Saving syncs slots: adds missing, deletes unticked (deleting a slot deletes its set entries — form help text says so).
- Context `controllers`: list of `Controller` with `slots` prefetched; card shows designator, name, part, firmware names; "Add controller" / "Edit" only when `can_edit`.

- [ ] **Step 1: Write failing tests** in `test_controllers.py` (lead, member engineer, viewer-outsider, project PWR, board; second project OTH with board):
  - `test_member_adds_controller_with_firmware`: POST create with `designator="U1", name="Main MCU", part="STM32G474RE", firmwares=[app, boot]` → controller exists, 2 slots, boot `flash_order == 1`, audit row `controller.created`.
  - `test_unticking_firmware_removes_slot`: edit with only `[app]` → 1 slot.
  - `test_outsider_cannot_edit`: outsider GET create → 403 or 404 (match `_project(edit=True)` behaviour).
  - `test_board_from_other_project_404`: create URL with key PWR and OTH's board id → 404.
  - `test_firmware_from_other_project_rejected`: posting OTH firmware id → form error, no controller.
  - `test_board_page_lists_controllers`: GET `projects:hw_board` contains "U1" and "Main MCU"; for a non-member viewer the "Add controller" link is absent.
- [ ] **Step 2: Run** `python manage.py test apps.firmware.tests.test_controllers` → FAIL.
- [ ] **Step 3: Implement views, form, templates, URLs**; delete is POST-only with a confirm button; audit `controller.updated` / `controller.deleted`; `log_activity(project, f"added controller {c} to {board.name}", actor=...)`.
- [ ] **Step 4: Run** the tests → PASS; also `python manage.py test apps.projects` → PASS (board page still renders).
- [ ] **Step 5: Commit** `git commit -m "Controllers on boards: add, edit, remove, with the firmware each runs"`.

### Task 4: Firmware set panel on the revision page

**Files:**
- Create: `templates/firmware/_firmware_set_card.html`
- Modify: `apps/firmware/board_views.py` (add `revision_firmware_set`, `revision_firmware_lock`), `apps/firmware/urls.py`, `apps/projects/views.py:475-500` (replace the `firmware` context with `set_rows(rev)`, `missing_count`), `templates/projects/revision_detail.html:64-66` (include the card)
- Test: `apps/firmware/tests/test_set_views.py`

**Interfaces:**
- Consumes: Task 2 `set_rows`, `save_set`, `set_lock`, `missing_count`.
- Produces URLs:
  - `projects/<key>/revisions/<int:pk>/firmware/` (POST) → `firmware:revision_set` — form fields `slot_<slot id>` = release id or `""`.
  - `projects/<key>/revisions/<int:pk>/firmware/lock/` (POST, `action=lock|unlock`) → `firmware:revision_set_lock`; requires `can_manage_projects` else 403.
- Card: table Controller | Firmware | Version (select preselects the entry, else `recommended`) | Issues (badges `bad`/`warn`, existing badge classes). If the board has no controllers: "Add the controllers on this board to choose their firmware." linking to the board page. Locked: selects disabled, "Locked by a lead" note, Unlock button for leads. "Download flashing bundle" link appears once Task 6 lands (Task 6 adds it).

- [ ] **Step 1: Write failing tests** (reuse the Task 2 fixture via a shared helper `make_fixture()` in `apps/firmware/tests/helpers.py`, returning a namespace with project, rev, slots, releases, users):
  - `test_member_saves_set`: POST `{slot_<boot>: boot100, slot_<app>: app100}` → redirect to revision, 2 entries, audit `revision.firmware_set`.
  - `test_viewer_cannot_save`: outsider POST → 403/404, no entries.
  - `test_revision_from_other_project_404`.
  - `test_engineer_cannot_lock`: engineer POST lock → 403; lead POST lock → `rev.firmware_set_locked` True, audit `revision.firmware_set_lock`.
  - `test_page_shows_rows_and_recall`: entry pinned to a release later recalled → revision page contains "Recalled — do not flash".
- [ ] **Step 2: Run** `python manage.py test apps.firmware.tests.test_set_views` → FAIL.
- [ ] **Step 3: Implement** the views (errors from `save_set` shown via `messages.error`, success "Firmware for {rev.title} saved."), the card and the context change.
- [ ] **Step 4: Run** the tests → PASS; `python manage.py test apps.projects apps.firmware` → PASS.
- [ ] **Step 5: Commit** `git commit -m "Revision page: choose and lock the firmware each controller ships with"`.

### Task 5: Builds use a firmware snapshot (`BuildFirmware`)

**Files:**
- Modify: `apps/production/models.py` (add `BuildFirmware`; add `BuildOrder.copy_firmware_from_revision()`, `BuildOrder.firmware_rows()`, `BuildOrder.has_recalled_firmware()`; remove `firmware_releases`)
- Create: `apps/production/migrations/0005_buildfirmware.py` (schema + `RunPython` copying M2M rows, reverse copies back), `apps/production/migrations/0006_remove_buildorder_firmware_releases.py`
- Modify call sites: `apps/production/views.py` (`BuildForm` drops `firmware_releases`; build create calls `copy_firmware_from_revision()`; `build_detail` uses `firmware_rows()` + `issues_for`; `build_action` `set_firmware` rewritten; recall checks use `has_recalled_firmware()`), `apps/production/fab_views.py:181,366`, `templates/production/build_detail.html:~118`, `templates/production/traveler.html:28`, `apps/firmware/views.py:224` (`builds` via `build_firmware__release=rel`, `.distinct()`), `apps/core/export.py:244`, `apps/core/management/commands/seed_demo.py:310`, `apps/production/tests/test_review_fixes.py:132,152`, `apps/firmware/tests/test_firmware.py:127,149`
- Test: `apps/production/tests/test_build_firmware.py`

**Interfaces:**
- Consumes: Task 1 models, Task 2 `issues_for`.
- Produces:
  - `production.BuildFirmware(build FK BuildOrder CASCADE related_name="build_firmware", slot FK firmware.ControllerSlot SET_NULL null blank, controller_label CharField(100, blank), release FK firmware.FirmwareRelease PROTECT related_name="build_firmware", flash_order PositiveIntegerField(default=1))`; ordering `["controller_label", "flash_order", "release__firmware__name"]`; `unique_together=[("build", "release")]`.
  - `BuildOrder.copy_firmware_from_revision() -> int` — replaces rows with one per `FirmwareSetEntry` of `self.revision` (label `str(slot.controller)`, flash order from slot); returns count. Called once after a build is created.
  - `BuildOrder.firmware_rows() -> QuerySet[BuildFirmware]` with `select_related("release__firmware")`, `prefetch_related("release__artifacts", "release__revisions")`.
  - `BuildOrder.has_recalled_firmware() -> bool`.
  - Build page editor (action `set_firmware`, only while `status in BuildOrder.ACTIVE`, `procurement_required` as today): per row a version select (`row_<id>` → release id of the *same firmware*, status released/testing); "Add firmware" select (`add` → any released/testing release of the project, creates a row with empty label); remove checkbox `remove_<id>`; button "Reset from revision" (`action=reset_firmware`) calls `copy_firmware_from_revision()`. All in `transaction.atomic()` with `select_for_update()` on the build; audit `build.firmware`; activity line as today.

- [ ] **Step 1: Write failing tests** in `test_build_firmware.py`:
  - `test_new_build_copies_revision_set`: revision with 2 set entries; POST build create → build has 2 `BuildFirmware` rows with labels "U1 Main MCU".
  - `test_build_snapshot_independent_of_revision`: after creating, change the revision set → build rows unchanged.
  - `test_build_snapshot_survives_controller_delete`: delete the controller → rows remain, `slot is None`, label kept.
  - `test_start_blocked_by_recalled_entry`: row release recalled → `build_action start` shows "This build uses recalled firmware…", status still planned.
  - `test_set_build_firmware_refuses_other_project`: `add` with OTH release id → no row added, warning message.
  - `test_set_build_firmware_version_must_match_firmware`: `row_<id>` set to a release of a different firmware → ignored with warning.
  - `test_migration_moves_build_firmware` (`TransactionTestCase` using `django.db.migrations.executor.MigrationExecutor`): migrate production to `0004_review_fixes`, create a build with M2M releases through the historical models, migrate to latest, assert the build has `BuildFirmware` rows for exactly those releases with empty labels. Restore latest migration state in `tearDown`.
- [ ] **Step 2: Run** `python manage.py test apps.production.tests.test_build_firmware` → FAIL.
- [ ] **Step 3: Implement** model, two migrations, helpers and every call site listed under Files. Update the two existing tests to create `BuildFirmware` rows instead of using the M2M. `seed_demo`: after demo controllers exist (Task 7 adds them), use `evt.copy_firmware_from_revision()`; until then create rows directly for `fw_app_13` and `fw_boot`.
- [ ] **Step 4: Run** `python manage.py test apps.production apps.firmware apps.core` → PASS; `grep -rn firmware_releases apps templates | grep -v migrations` → only `Revision.firmware_releases` (the compatibility M2M) remains; `makemigrations --check --dry-run` clean.
- [ ] **Step 5: Commit** `git commit -m "Builds keep a snapshot of the revision's firmware set; migrate existing build firmware"`.

### Task 6: Flashing bundle download

**Files:**
- Create: `apps/firmware/bundle.py`
- Modify: `apps/firmware/board_views.py` (views `revision_bundle`, `build_bundle`), `apps/firmware/urls.py`, `templates/firmware/_firmware_set_card.html` and `templates/production/build_detail.html` (download links)
- Test: `apps/firmware/tests/test_bundle.py`

**Interfaces:**
- Consumes: `FirmwareSetEntry` (revision), `BuildOrder.firmware_rows()` (Task 5), `FirmwareArtifact`.
- Produces:
  - `BundleRow = namedtuple("BundleRow", "label designator release flash_order")`.
  - `write_bundle(rows: list[BundleRow], fh) -> dict` — writes a zip to the open binary file `fh`: each artifact of kind `image`, `bootloader` or `package` at `"<NN>-<designator or 'board'>-<firmware slug>-<version>/<artifact name>"` (`NN` = 2-digit flash sequence across the bundle, sorted by designator then flash_order), plus `manifest.json` = `{"title", "created", "items": [{"controller", "designator", "firmware", "version", "status", "file", "sha256", "size", "flash_order"}]}`; returns the manifest dict. Raises `BundleError(message)` if any release is recalled ("{firmware} {version} is recalled. Choose another version first."), if there are no files ("There are no firmware files to download yet."), or if the total artifact size exceeds `settings.MAX_UPLOAD_MB` MB.
  - URLs: `projects/<key>/revisions/<int:pk>/firmware/bundle/` → `firmware:revision_bundle`; `production/builds/<int:pk>/firmware-bundle/` → `firmware:build_bundle`. Both GET, `can_view` check, written to a `tempfile.TemporaryFile(dir=settings.DATA_DIR)`, served as `FileResponse(as_attachment=True, filename=f"{project.key}-{board/rev slug}-firmware.zip")` with `X-Content-Type-Options: nosniff`. `BundleError` → `messages.error` + redirect back.
- [ ] **Step 1: Write failing tests** (fixture with two slots, each release having one `.bin` artifact and one `.elf` debug artifact):
  - `test_manifest_and_files`: unzip the response; names start `01-U1-bootloader-1.0.0/` and `02-U1-main-application-1.0.0/`; `.elf` absent; manifest sha256 equals `FirmwareArtifact.sha256`.
  - `test_bundle_refuses_recalled`: recall one pinned release → redirect with message containing "is recalled".
  - `test_bundle_size_cap`: `override_settings(MAX_UPLOAD_MB=0)` → redirect with size message.
  - `test_bundle_permissions`: outsider → 403/404; other-project id → 404.
  - `test_build_bundle_uses_snapshot`: build bundle lists the build's rows even after the revision set changed.
- [ ] **Step 2: Run** `python manage.py test apps.firmware.tests.test_bundle` → FAIL.
- [ ] **Step 3: Implement** `bundle.py`, views, URLs, links.
- [ ] **Step 4: Run** the tests → PASS.
- [ ] **Step 5: Commit** `git commit -m "Download a flashing bundle (images + manifest) per revision and build"`.

### Task 7: Firmware "used on", demo data, help, README and release 1.5.0

**Files:**
- Modify: `apps/firmware/views.py` (`firmware_detail` context `used_on`: controllers via `fw.slots`, revisions via `FirmwareSetEntry`), `templates/firmware/firmware_detail.html`, `templates/firmware/release_detail.html` (set entries + builds using this release)
- Modify: `apps/core/management/commands/seed_demo.py` (controllers: PWR power board `U1 Main MCU STM32G474RE` → Bootloader + Main application; Rev B firmware set pinned; EVT build via `copy_firmware_from_revision()`)
- Modify: `templates/core/help/firmware.html`, `templates/core/help/hardware.html`, `templates/core/help/production.html`, `README.md` (feature list + "What's new in 1.5.0"), `VERSION` → `1.5.0`
- Modify: `docs/superpowers/specs/2026-10-19-sources-and-controllers-design.md` — recall warnings shown on revision, build and release pages (not the dashboard)
- Test: `apps/firmware/tests/test_set_views.py` (`test_firmware_page_shows_used_on`), `apps/core/tests` existing seed test (run)

- [ ] **Step 1: Write failing test** `test_firmware_page_shows_used_on`: firmware page contains "U1 Main MCU" and the revision title of a pinned set.
- [ ] **Step 2: Implement** context + templates; demo data; help text (plain English: what controllers are, how to choose firmware per revision, that builds keep a copy, how to download the bundle); README.
- [ ] **Step 3: Full verification**:
  - `python manage.py test apps` → all pass
  - `ruff check apps config --select F,E9` → no errors
  - `python manage.py makemigrations --check --dry-run` → "No changes detected"
  - `rm -f` a scratch SQLite DB under a temp `WORKBENCH_DATA_DIR`, then `migrate && seed_demo` → completes; `runserver` and open the board, revision and build pages for PWR, then download the Rev B bundle and inspect `manifest.json`.
  - `docker build -t workbench-test .` → succeeds.
- [ ] **Step 4: Commit and release**:
  ```bash
  git commit -am "Release 1.5.0: board controllers and firmware sets"
  git tag -a v1.5.0 -m "Workbench 1.5.0"
  git push origin main --tags
  git -C /mnt/hgfs/GitHub/project-workbench/workbench pull --ff-only
  ```
  Then check GitHub Actions CI is green (`gh run list --limit 1`).

# Workbench

Workbench is one web app for a remote embedded-hardware team. It covers:

- team chat
- projects and task boards
- design and code reviews linked to GitHub
- a file library
- firmware releases per board revision, with version control and checksums
- PCB design files per revision, with a built-in Gerber viewer
- assembly-house (JLCPCB, Seeed…) builds with an in-house finishing checklist
- a parts library with KiCad BOM import
- purchasing and production builds
- timesheets and weekly reports

Every feature is built into this codebase, with no third-party apps embedded. It runs as **one Docker container**, and everything it stores sits in **one `data` folder**.

---

## Quick start (about 5 minutes)

You need [Docker](https://docs.docker.com/get-docker/). On Windows or Mac, install Docker Desktop.

```bash
# in the Workbench folder
docker compose up -d
docker compose logs workbench      # shows your one-time setup code
```

Open **http://localhost:8000** (or `http://<server-ip>:8000`) and enter the setup code. Then create your administrator account and scan the QR code with an authenticator app on your phone. That's it.

There are no passwords, keys or config files to prepare. Workbench generates its own secrets on first start and keeps them in the data folder.

### On a server, straight from your private GitHub repository (recommended)

```bash
scp install.sh you@server:            # copy the one script to the new server
ssh you@server
bash install.sh git@github.com:YOUR-ORG/workbench.git
```

The script installs git and Docker, creates a **read-only deploy key** and shows it. Add it in GitHub under *Settings → Deploy keys* (leave write access off), press Enter, and it verifies GitHub's host fingerprint, clones the newest release into `/opt/workbench`, asks for your domain (automatic HTTPS), starts Workbench and prints the setup code.

Update later with:

```bash
/opt/workbench/update.sh --check      # is there a new version?
/opt/workbench/update.sh              # backup → download → build → restart → health check
```

If the new version doesn't come up healthy, `update.sh` puts the previous version back and restores the backup automatically. The server follows release tags (`v1.3.0`), so pushes to `main` don't reach it until you tag a release; `install.sh --branch main` follows a branch instead. An existing HTTPS clone can switch to a deploy key with `./install.sh --deploy-key`.

### On an internet-facing server, with HTTPS (manual)

On a fresh Ubuntu or Debian server, first point a domain (e.g. `workbench.yourcompany.com`) at the server, then:

```bash
./install.sh
```

The script:

1. installs Docker if it's missing
2. asks for your domain
3. turns on automatic HTTPS (a free Let's Encrypt certificate, renewed automatically)
4. starts everything and prints the setup code

To do the same by hand:

```bash
echo "WORKBENCH_DOMAIN=workbench.yourcompany.com" > .env
echo "WORKBENCH_BIND=127.0.0.1" >> .env
docker compose --profile https up -d
```

Without a domain, Workbench still works over plain HTTP. That's fine inside an office network, a VPN or Tailscale; administrators see a reminder banner.

---

## The data folder

Everything Workbench stores lives in `./data` next to `docker-compose.yml`:

| Folder / file | What's in it |
|---|---|
| `data/db/` | The database (SQLite, a single file) |
| `data/files/` | Every uploaded file, one folder per project (`files/PWR/…`, `files/shared/…`), all versions kept |
| `data/backups/` | Nightly backup zips |
| `data/secrets.json` | Generated keys. They're needed to read 2FA secrets, so never lose this file |

- **Back up:** automatic every night, or press *Back up now* under *System & backups*. Each zip holds the database, all files and the keys. Download backups from the same page, or sync `data/backups` to cloud storage. Keep a copy off the server.
- **Restore:**
  ```bash
  docker compose stop workbench
  docker compose run --rm workbench restore workbench-YYYYMMDD-HHMMSS.zip
  docker compose start workbench
  ```
  Your data from before the restore is kept in `data/pre-restore-…` until you delete it.
- **Move to a new server:** stop Workbench, copy the `data` folder across, run `docker compose up -d`.
- **Update:** `./update.sh` (backup, pull, rebuild, health check, automatic rollback). Database changes apply automatically on start.
- **Download everything:** *System & backups → Download everything* builds one zip organised for people rather than for restoring: `Projects/<project>/Files`, `Firmware/<name>/<version>`, `Revisions` (BOMs as CSV), `Tasks.csv`, chat transcripts of public channels, parts, orders and builds as CSV files. Private channels, direct messages and keys are left out. Use a backup zip to restore; use this to hand files to someone or archive them.
- **Storage housekeeping:** *Files → Storage* shows usage by project and the largest files. It can also delete old versions or empty the trash. Deleted files stay in the trash for 30 days first.

### Where uploaded files are kept

Chosen by an administrator in **System → Storage**: a folder on the server (default `data/files`), or cloud storage over the S3 protocol — **Amazon S3, Cloudflare R2, Backblaze B2, Wasabi, DigitalOcean Spaces, Google Cloud Storage** (HMAC keys) or a self-hosted **MinIO / Synology / QNAP** server. Enter the bucket and keys, *Save and test connection*, then *Move files and switch*: every file is copied and verified in the background and Workbench switches only when all of them made it. Ticking *Lock storage here* makes the choice permanent in the app (`manage storage --unlock` on the server undoes it). Keys are encrypted; buckets stay private; downloads always go through Workbench's permission checks; backups include cloud files and a copy is kept in the bucket.

```bash
docker compose exec workbench manage storage          # where files are kept, locked or not
docker compose exec workbench manage storage --test   # write/read/delete a test file
```

`WORKBENCH_STORAGE=s3` and friends in `.env` (version 1.2) still work and take precedence over the page.

Useful commands:

```bash
docker compose logs -f workbench                 # watch the log
docker compose exec workbench manage backup_now  # take a backup from the command line
docker compose exec workbench manage seed_demo   # load example data (empty installs only)
curl http://localhost:8000/healthz               # health check for monitoring
```

---

## What's inside

| Area | Highlights |
|---|---|
| **Projects** | A task board per product/board, revisions (Rev A, Rev B) with a **release checklist** that must be signed off before a revision is released, reviewers, due dates, **blocked** flags, comments with @mentions |
| **Chat** | A channel for each project (automatic), topic channels, private channels, direct messages, file sharing, GitHub/task updates posted automatically |
| **Files** | Project and company-wide spaces, folders, drag-and-drop upload with progress, **every version kept**, previews for PDFs, images and text/CSV/KiCad files, trash with restore, storage dashboard |
| **GitHub** | Signed webhooks link PRs and commits to tasks by ID (`PWR-12`). PR opened → *In review*, changes requested → *In progress*, merged → *Done*; CI (e.g. KiBot ERC/DRC) results shown and failures announced |
| **Firmware** | Per project: firmware components (main app, bootloader…), releases with **semantic versions** (`1.4.0`, `1.4.0-rc.1`), release notes and a **changelog between any two versions**, files with **SHA-256 checksums**, and the **board revisions each release is compatible with**. Draft → Testing → **Released** (leads approve; released files are locked) → Deprecated / **Recalled** (with a reason; everyone is notified and builds using it are blocked). Each revision shows its recommended firmware; builds record which firmware was flashed; a GitHub release tag can create a Testing release automatically |
| **Design files** | Per revision: Gerbers, drill, schematic and PCB files (KiCad, Eagle, Altium), pick-and-place, STEP, PDFs — sorted by type, every version kept. A **built-in Gerber viewer** (our own RS-274X/Excellon renderer) shows top/bottom with mask colour, a layer view, zoom, pan and measure, plus board size, layer count, smallest drill/track and hole counts |
| **Parts & BOM** | Parts library with stock history, suppliers, KiCad/KiBot BOM import with matching, cost per board, boards buildable from stock, BOM comparison between revisions |
| **Production** | Purchase orders (draft → ordered → received, updating stock and cost), builds with shortage check, one-click orders for missing parts, test yield. **Assembly-house builds:** mark BOM lines as fitted by the assembler, in house or not fitted; download JLCPCB/Seeed BOM + CPL files with in-house parts left out; track the order; on arrival only in-house parts leave stock and the build becomes a **finishing checklist** (per-part steps highlighted on the board, +1/+5 per board, phone-friendly) with a printable traveler |
| **Timesheets & reports** | Log hours per task; a **weekly report** of hours by person and project, finished work, reviews, blockers and builds, printable or as CSV |
| **Home** | Your tasks, reviews waiting for you, blocked and overdue work, low stock, and a **team clock** showing who's in working hours (Toronto ↔ Karachi) |
| **Administration** | People and roles, invite links (or email them), *System & backups* for settings, **company logo** (SVG or transparent PNG, with an optional version for the dark menu bar), SMTP email, GitHub secret, backups and health, audit log |
| **Help** | Built-in guides for every area, a getting-started checklist, and a short explanation on every page |

## Security

- **Sign-in:** a password of 12+ characters plus **mandatory authenticator-app 2FA**. Each code works only once, and there are single-use recovery codes. Accounts lock after 5 failed attempts.
- **Access:** five roles. Engineers see only their projects. Released revisions are locked to leads. Every page and file download is permission-checked on the server.
- **Protection at rest:** 2FA, SMTP and webhook secrets are encrypted in the database. Uploaded files are never served directly, and files that could run in a browser (`.html`, `.js`, `.exe`…) are refused. Uploaded SVG logos are cleaned (scripts, event handlers and external links removed) and served with a sandboxing policy.
- **Browser protections:** HTTPS with HSTS (in HTTPS mode), secure cookies, CSRF protection, and a strict Content-Security-Policy. No third-party scripts, fonts or trackers are loaded.
- **GitHub webhook:** HMAC-SHA256 signatures, replay protection, and capped logging of rejected requests.
- **Audit log:** sign-ins (including failures), permission changes, uploads, downloads, backups, BOM imports, stock changes and orders, each with its IP address.
- **Container:** runs as an unprivileged user, and invite links always use the configured address, never the request's Host header.

Report security issues privately to the maintainer.

---

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export WORKBENCH_DEBUG=1
python manage.py migrate && python manage.py seed_demo
python manage.py runserver                     # http://localhost:8000 — haris / Workbench-demo-2026!
python manage.py test apps                     # 158 tests (S3 is tested against a local mock)
```

The demo users are `haris` (admin), `ayesha` (lead), `bilal`, `sana` and `usman` (engineers), and `fatima` (procurement). They all use the password `Workbench-demo-2026!`.

### Version control workflow

- `main` is always deployable. Work on a branch named after the task, e.g. `wb-14-export-po-pdf`, and open a pull request.
- CI (`.github/workflows/ci.yml`) runs the linter and tests, Django's production security check, a dependency audit, and a container build with a health check. A PR needs CI to pass and one review before it's merged.
- Database changes go in migrations (`python manage.py makemigrations`), committed with the code.
- Tag releases (`git tag v1.2.0 && git push --tags`). `.github/workflows/release.yml` then publishes a ready-made image to GitHub Container Registry, so servers can use `image: ghcr.io/<owner>/workbench:1.2.0` instead of building.

### Project layout

```
config/              settings (zero-config; data folder, generated secrets)
apps/accounts/       users, roles, 2FA, invites, lockout
apps/core/           dashboard, search, notifications, audit log, help, setup wizard,
                     system page, backups, export, storage, logo, scheduler, email
apps/projects/       projects, revisions & release checklists, tasks, activity
apps/chat/           channels, messages, file sharing
apps/files/          file library: spaces, folders, versions, trash, storage
apps/inventory/      parts, suppliers, stock, BOMs, KiCad import
apps/firmware/       firmware components, releases, artifacts
apps/design/         revision design files, Gerber/Excellon parser and board renderer
apps/production/     purchase orders, builds, assembly-house files, finishing
apps/timesheets/     time entries, weekly report
apps/integrations/   GitHub webhook
templates/ static/   UI (hand-written CSS and JavaScript, no frameworks)
Dockerfile docker-compose.yml docker-entrypoint.sh deploy/
install.sh update.sh  server install (deploy key) and safe updates
docs/examples/       sample BOM, KiBot workflow for hardware repos
```

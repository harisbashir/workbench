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

**Contents:** [Quick start](#quick-start-about-5-minutes) · [Deploying to a server](#deploying-to-a-server-step-by-step) · [Updating](#updating) · [The data folder](#the-data-folder) · [Troubleshooting](#troubleshooting) · [What's inside](#whats-inside) · [Security](#security) · [Development](#development)

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

That's the quickest way to try it on your own computer. For a real server, follow [Deploying to a server](#deploying-to-a-server-step-by-step).

---

## Deploying to a server, step by step

In the commands below, replace `harisbashir/workbench` with your GitHub repository.

### What you need

- A server running **Ubuntu 22.04 / 24.04 or Debian 12** with at least **2 GB RAM and 20 GB disk** (a small cloud VM is enough for a team of 20), and a user that can run `sudo`.
- For access over the internet: a **domain name** (e.g. `workbench.yourcompany.com`) with a DNS **A record** pointing at the server's public IP. Check it before you start — this must print the server's IP:
  ```bash
  getent hosts workbench.yourcompany.com
  ```
- Without a domain, Workbench runs over plain HTTP on port 8000. Only do that inside an office network, a VPN or Tailscale.

### Step 1 — Log in and open the firewall

```bash
ssh you@SERVER-IP
sudo apt update && sudo apt install -y curl
sudo ufw allow OpenSSH
sudo ufw allow 80
sudo ufw allow 443
# sudo ufw allow 8000        # only if you're NOT using a domain
sudo ufw --force enable
```

If your cloud provider has its own firewall (AWS security groups, DigitalOcean/Hetzner firewalls…), open the same ports there too.

### Step 2 — Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/harisbashir/workbench/main/install.sh) https://github.com/harisbashir/workbench.git
```

The installer:

1. installs git and Docker if they're missing
2. downloads the newest release (the highest `v…` tag) into `/opt/workbench`
3. asks for your **domain** — type it for automatic HTTPS (Let's Encrypt), or leave it empty for `http://SERVER-IP:8000`
4. builds and starts Workbench (the first build takes a few minutes)
5. prints the address and a one-time **setup code**

Options: `--dir /srv/workbench` (another folder), `--branch main` (follow a branch instead of release tags), `--domain workbench.yourcompany.com` (skip the question), `--no-https`.

**Private repository?** Copy the script to the server and give it the SSH address. It creates a read-only deploy key, shows it, and waits while you add it in GitHub under *Repository → Settings → Deploy keys → Add deploy key* (leave *Allow write access* off):

```bash
scp install.sh you@SERVER-IP:        # run on your computer
bash install.sh git@github.com:harisbashir/workbench.git
```

### Step 3 — First sign-in

1. Open the address the installer printed.
2. Enter the setup code. (Lost it? `cd /opt/workbench && docker compose logs workbench | grep -i setup`)
3. Create your administrator account and scan the QR code with an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password…). **Save the recovery codes.**

### Step 4 — Set it up

In **System & backups**:

| Tab | What to do |
|---|---|
| General | Company name, time zone (for nightly backups), **logo** |
| Storage | Keep files on the server, or move them to S3 / R2 / B2 / Wasabi / your NAS — *Save and test connection*, then *Move files and switch* |
| Backups | Check the nightly backup time; later, download a backup and keep it off the server |
| Email | SMTP details so invites and notifications can be emailed; *Send me a test email* |
| GitHub | Copy the webhook address and secret into each repository (*Settings → Webhooks*) |

Then invite your team under **People → Invite**.

### Step 5 — Check it's healthy

```bash
cd /opt/workbench
docker compose ps                         # "workbench" should say (healthy)
curl -s http://localhost:8000/healthz     # {"ok": true, ...}
```

Point an uptime monitor (UptimeRobot, Healthchecks…) at `https://your-domain/healthz`.

---

## Updating

### Servers installed with `install.sh`

```bash
cd /opt/workbench
./update.sh --check       # is there a new version? (lists the changes)
./update.sh               # update
./update.sh --to v1.3.1   # go to a specific newer version
```

`update.sh`:

1. takes a backup (`data/backups/…`)
2. downloads the new version from GitHub
3. builds it while the current version keeps running
4. restarts and waits for the health check
5. **if the new version doesn't come up healthy, puts the previous version back and restores the backup**

Everything it does is logged in `data/update.log`. Workbench is unavailable for about a minute during step 4.

### Older servers set up with `git clone` (before 1.3)

These don't have `update.sh` yet, so update once by hand:

```bash
cd ~/workbench                                                       # wherever you cloned it
docker compose exec workbench manage backup_now                      # safety backup
git remote set-url origin https://github.com/harisbashir/workbench.git  # the public address
git pull
docker compose up -d --build            # add --profile https if you use a domain
docker compose ps                       # wait for (healthy)
```

After that, use `./update.sh` like any other server. If `git pull` complains about local changes, run `git stash` first.

### Publishing a new version

Servers installed with `install.sh` follow **release tags**, so pushing to `main` alone doesn't reach them:

```bash
# on your computer, after the changes are merged into main
echo 1.3.2 > VERSION
git commit -am "Release 1.3.2"
git tag -a v1.3.2 -m "Workbench 1.3.2"
git push origin main --tags
```

Then run `./update.sh` on each server. A server installed with `--branch main` (or an old `git clone`) follows `main` directly — handy for a test server.

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

## Troubleshooting

Run these on the server, in the Workbench folder (`/opt/workbench`, or wherever you cloned it). If you use a domain, add `--profile https` to `docker compose up`/`down` commands so the HTTPS proxy (Caddy) is included; `ps`, `logs` and `exec` work without it.

### First look

```bash
cd /opt/workbench
docker compose ps                                   # is it running and (healthy)?
docker compose logs --tail 100 workbench            # recent log of the app
docker compose logs -f workbench                    # follow the log live (Ctrl+C to stop)
curl -s http://localhost:8000/healthz               # {"ok": true, "database": true, "disk_free_mb": ..., "scheduler_seen": ..., "version": ...}
tail -n 50 data/update.log                          # what the last updates did
docker compose exec workbench manage check --deploy # Django's own configuration check
```

Without a domain, `check --deploy` warns about secure cookies and HSTS; that's expected on plain HTTP.

`/healthz` reports `"ok": false` when the database can't be reached or less than 200 MB of disk is free. `scheduler_seen` is the last time the background worker (nightly backups, emails, exports, storage moves) checked in; it should be within the last minute or two.

### Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| The browser can't connect at all | Firewall, or the container isn't running | `docker compose ps`; open ports 80/443 (or 8000) in `ufw` **and** in your cloud provider's firewall; `docker compose up -d` |
| HTTPS certificate error, or the domain doesn't load | DNS not pointing at the server yet, or ports 80/443 closed, so Let's Encrypt couldn't verify | `getent hosts your-domain` must show the server's IP; then `docker compose --profile https restart caddy` and `docker compose logs caddy` |
| **Bad Request (400)** | Opening by IP address while a domain is set | Use the domain name, or change `WORKBENCH_DOMAIN` in `.env` (see below) |
| **CSRF verification failed** when signing in | Workbench sits behind another proxy/load balancer with a different address | Add `WORKBENCH_CSRF_TRUSTED_ORIGINS=https://the-address-people-use` and `WORKBENCH_ALLOWED_HOSTS=the-address-people-use` to `.env`, then `docker compose up -d` |
| Invite or email links point to the wrong address | *Site url* not set | *System & backups → General → Site url*, e.g. `https://workbench.yourcompany.com` |
| Port 8000 is already in use | Another program uses it | Set `WORKBENCH_PORT=8080` in `.env`, then `docker compose up -d` |
| Container restarts over and over | A startup error | `docker compose logs --tail 200 workbench` — the error is at the end |
| Nightly backups or "Download everything" don't happen | Background worker stopped | `docker compose restart workbench`; check `scheduler_seen` in `/healthz`; run one pass by hand with `docker compose exec workbench manage run_scheduler --once` |
| Uploads fail / "disk full" | Disk or cloud storage problem | `df -h`, `du -sh data/*`; *Files → Storage* to delete old versions and empty the trash; `docker system prune` removes old Docker images; `docker compose exec workbench manage storage --test` checks cloud storage |
| Emails don't arrive | SMTP settings | *System & backups → Email → Send me a test email*; the reason is shown on the page |
| GitHub events don't show up | Webhook address or secret | In GitHub: *Repository → Settings → Webhooks → Recent deliveries* shows each attempt and the response; the address and secret must match *System & backups → GitHub* |
| `update.sh` says "Couldn't reach GitHub" | Network, or the wrong repository address | `git remote -v`; for a public repository: `git remote set-url origin https://github.com/harisbashir/workbench.git` |
| `./update.sh: Permission denied` | The script lost its "executable" flag (e.g. uploaded through the GitHub website or from Windows) | `chmod +x install.sh update.sh`, or run it as `bash update.sh` |
| `update.sh` says files were changed on the server | Someone edited files in the folder | `git status` to see them; `git stash` to set them aside (or `git checkout -- .` to throw them away) |

### Locked out

```bash
docker compose exec workbench manage reset_account --list               # all accounts; shows LOCKED ones
docker compose exec workbench manage reset_account haris                # unlock after too many wrong passwords
docker compose exec workbench manage reset_account haris --password     # + prints a one-time link to set a new password
docker compose exec workbench manage reset_account haris --2fa          # + turns off 2FA (set up again at next sign-in)
docker compose exec workbench manage create_admin --username newadmin   # a new administrator, with a set-password link
```

Every reset is written to the audit log. Other people's accounts can also be unlocked, and their 2FA reset, by an administrator under **People**.

### Change the domain later (or add one)

```bash
cd /opt/workbench
./install.sh              # asks for the domain again and restarts
```

Or by hand: edit `WORKBENCH_DOMAIN=` (and `WORKBENCH_BIND=127.0.0.1`) in `.env`, then `docker compose --profile https up -d`.

### Go back to the previous version

`update.sh` does this by itself when an update fails. To do it by hand (for example, the new version starts but something doesn't work right):

```bash
cd /opt/workbench
ls -t data/backups | head -3          # the newest one is the backup update.sh took
git tag --sort=-v:refname | head -5   # versions
git checkout v1.3.0                   # the version you were on before
docker compose stop workbench
docker compose run --rm workbench restore workbench-YYYYMMDD-HHMMSS.zip
docker compose up -d --build
```

Restore the backup together with the old version: a newer version may have changed the database, and an older version can't read it. Anything added since that backup is lost; the data from just before the restore is kept in `data/pre-restore-…`.

### Useful commands

```bash
docker compose exec workbench manage backup_now          # backup now
docker compose exec workbench manage storage             # where files are kept, locked or not
docker compose exec workbench manage storage --test      # write/read/delete a test file
docker compose exec workbench manage storage --unlock    # allow changing locked storage again
docker compose exec workbench manage shell               # Python shell with Workbench loaded (careful)
docker compose restart workbench                         # restart the app
docker compose build --no-cache && docker compose up -d  # rebuild from scratch
docker stats --no-stream                                 # CPU and memory use
```

Never set `WORKBENCH_DEBUG=1` on a server: it shows internal details to anyone who hits an error. Use the logs instead.

### Asking for help

Include the output of these (they contain no passwords or keys):

```bash
cat VERSION; git log --oneline -1
docker compose ps
curl -s http://localhost:8000/healthz
docker compose logs --tail 200 workbench
tail -n 50 data/update.log
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
python manage.py test apps                     # 159 tests (S3 is tested against a local mock)
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

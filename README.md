# Workbench

One web app for a remote embedded-hardware team. It covers team chat, project and task management, design and code reviews linked to GitHub, a parts library with KiCad BOM import, purchasing, and production builds.

Every feature is built in this codebase. There is no embedded third-party chat, project tool or ERP, and the pages load no external scripts, fonts or trackers.

| Module | What it does |
|---|---|
| **Projects** | Projects (e.g. `PWR`) with board revisions (Rev A, Rev B), a drag-and-drop task board, reviewers, due dates, comments with @mentions, and file attachments |
| **Chat** | A channel for each project (created automatically), topic channels, private channels and direct messages. GitHub and task updates are posted automatically |
| **GitHub** | Signed webhooks link pull requests and commits to tasks by ID (e.g. `PWR-12`). PR opened → *In review*, changes requested → *In progress*, merged → *Done*. CI results such as KiBot ERC/DRC are shown and failures are announced |
| **Parts & BOM** | Parts library with stock and stock history, suppliers, per-revision BOMs imported from KiCad/KiBot CSV, cost per board, boards buildable from stock, and BOM comparison between revisions |
| **Production** | Purchase orders (draft → ordered → received, updating stock and cost) and build orders (shortage check, one-click orders for missing parts, stock consumption, test yield) |
| **Security** | Mandatory two-factor sign-in, five roles, account lockout, invite links, audit log, strict Content-Security-Policy, and access-checked file downloads |
| **Help** | Built-in guides for every area, a getting-started checklist, and explanations on each page |

---

## Try it locally (5 minutes)

You need Python 3.11+ and Git.

```bash
git clone <your-repo-url> workbench && cd workbench
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export WORKBENCH_DEBUG=1                                 # Windows: set WORKBENCH_DEBUG=1
python manage.py migrate
python manage.py seed_demo                               # example team, projects, parts, orders
python manage.py runserver
```

Open http://localhost:8000 and sign in as `haris` with password `Workbench-demo-2026!`. The first sign-in asks you to scan a QR code with an authenticator app (Google Authenticator, Microsoft Authenticator, Authy).

The other demo users are `ayesha` (lead), `bilal`, `sana`, `usman` (engineers) and `fatima` (procurement). They all use the same password.

To start clean instead of using demo data:

```bash
python manage.py migrate
python manage.py create_admin --username haris --first-name Haris --email you@example.com
# open the printed link to choose your password
```

## Run the tests

```bash
WORKBENCH_DEBUG=1 python manage.py test apps
```

There are 59 tests. They cover sign-in and 2FA, lockout, per-role permissions, file access, chat HTML escaping, BOM import, stock/PO/build flows, and the GitHub webhook (signatures, replay protection, task automation). CI runs them on every push, together with Django's production security check and a dependency vulnerability audit (see `.github/workflows/ci.yml`).

---

## Deploying to production

The included Docker setup runs **Caddy** (automatic HTTPS from Let's Encrypt), **Workbench** (gunicorn) and **PostgreSQL**. A small cloud VM is enough to start, for example 2 vCPU and 4 GB RAM on DigitalOcean, Hetzner, AWS Lightsail or Azure.

1. Point a domain (e.g. `workbench.yourcompany.com`) at the server's IP address.
2. Install Docker, then on the server:
   ```bash
   git clone <your-repo-url> /opt/workbench && cd /opt/workbench
   cp .env.example .env
   nano .env            # fill in every value — see below
   docker compose up -d --build
   docker compose exec web python manage.py create_admin --username haris --first-name Haris
   ```
3. Open the printed link, set your password and set up two-factor sign-in.
4. Add your team under **People** and send each person their sign-in link.

Values in `.env` that you must set:

| Variable | How to generate / what to put |
|---|---|
| `WORKBENCH_SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `WORKBENCH_FIELD_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. It encrypts 2FA secrets, so back it up |
| `WORKBENCH_GITHUB_WEBHOOK_SECRET` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `WORKBENCH_DB_PASSWORD` | A long random password |
| `WORKBENCH_DOMAIN`, `WORKBENCH_ALLOWED_HOSTS` | Your domain, e.g. `workbench.yourcompany.com` |
| `WORKBENCH_CSRF_TRUSTED_ORIGINS`, `WORKBENCH_SITE_URL` | `https://` + your domain |

**Backups:** `deploy/backup.sh` dumps the database and uploaded files. Schedule it nightly with cron, copy the `backups/` folder off the server, and test a restore now and then.

**Updating:** `git pull && docker compose up -d --build`. Database migrations run automatically on start.

## Connecting GitHub

See **Help → Connecting GitHub** in the app. In short:

1. Set `WORKBENCH_GITHUB_WEBHOOK_SECRET` on the server.
2. In each project's settings, enter its repository (`owner/name`).
3. In GitHub, go to repo (or organisation) → Settings → Webhooks and add the Payload URL shown on Workbench's GitHub page. Use content type `application/json`, the same secret, and these events: Pull requests, Pull request reviews, Pushes, Workflow runs.

For hardware repos, copy `docs/examples/kibot.yml` and `docs/examples/.kibot.yaml`. Every pull request then gets ERC/DRC checks, a schematic PDF, Gerbers and a BOM CSV you can import into Workbench.

---

## Security summary

- **Authentication:** passwords of 12+ characters checked against a common-password list, plus mandatory TOTP two-factor sign-in with 8 single-use hashed recovery codes. 2FA secrets are encrypted at rest (Fernet). Accounts lock after 5 failed attempts.
- **Authorisation:** five roles. Engineers and viewers only see projects they belong to. Released revisions are locked to leads. Every view checks permissions on the server.
- **Transport and browser:** HTTPS only with HSTS, secure/HttpOnly/SameSite cookies, CSRF protection, a strict Content-Security-Policy (`'self'` only, no inline scripts or styles), `X-Frame-Options: DENY`, and `no-store` caching for signed-in pages.
- **Input handling:** chat, comment and task text is HTML-escaped before any formatting is applied. Uploads are restricted to an allow-list of file types and a size limit, and served only through a permission-checked view.
- **GitHub webhook:** HMAC-SHA256 signature verification, delivery-ID replay protection, capped logging of rejected requests, and each event applied atomically.
- **Accountability:** an append-only audit log records sign-ins (including failures), permission changes, uploads, BOM imports, stock changes and orders, with IP address.

Report security issues privately to the maintainer. Don't open a public issue.

## Project layout

```
config/              settings, URLs
apps/accounts/       users, roles, 2FA, invites, lockout
apps/core/           dashboard, search, notifications, audit log, help, security middleware
apps/projects/       projects, revisions, tasks, comments, attachments, activity
apps/chat/           channels, messages, safe formatting
apps/inventory/      parts, suppliers, stock, BOMs, KiCad import
apps/production/     purchase orders, build orders
apps/integrations/   GitHub webhook
templates/ static/   UI (hand-written CSS and JavaScript, no frameworks)
deploy/              Caddy config and backup script
docs/examples/       sample BOM, KiBot workflow
```

## Contributing (version-control workflow)

- `main` is always deployable. Don't commit to it directly.
- Create a branch for each change, named after the task, e.g. `wb-14-export-po-pdf`.
- Open a pull request. CI must pass and one person must review it before merging.
- Add or update tests with every change. Run `python manage.py test apps` before pushing.
- Database changes go in migrations: `python manage.py makemigrations`. Commit them with the code.
- Tag releases (`git tag v1.1.0`) and deploy from tags.

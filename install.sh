#!/usr/bin/env bash
# Workbench installer for a fresh Ubuntu/Debian server (run from the Workbench folder).
#   ./install.sh
# It installs Docker if needed, asks whether to use a domain with HTTPS,
# starts Workbench and prints the setup code.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker…"
  curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required (docker compose). Please update Docker." >&2
  exit 1
fi
SUDO=""
if ! docker info >/dev/null 2>&1; then SUDO="sudo"; fi

touch .env
current_domain=$(grep -E '^WORKBENCH_DOMAIN=' .env | cut -d= -f2- || true)
say "Domain name"
echo "If people will reach Workbench over the internet, enter the domain you've pointed at this server"
echo "(e.g. workbench.yourcompany.com) to turn on HTTPS automatically. Leave empty to use http://<server-ip>:8000"
read -r -p "Domain [${current_domain}]: " domain
domain="${domain:-$current_domain}"

grep -v -E '^(WORKBENCH_DOMAIN|WORKBENCH_BIND)=' .env > .env.tmp || true
mv .env.tmp .env
profile=()
if [ -n "$domain" ]; then
  echo "WORKBENCH_DOMAIN=$domain" >> .env
  echo "WORKBENCH_BIND=127.0.0.1" >> .env
  profile=(--profile https)
  url="https://$domain"
else
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  url="http://${ip:-localhost}:8000"
fi

mkdir -p data
say "Building and starting Workbench (the first build takes a few minutes)…"
$SUDO docker compose "${profile[@]}" up -d --build

say "Waiting for Workbench to start…"
for _ in $(seq 1 60); do
  if $SUDO docker compose exec -T workbench python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

code=$(python3 -c "import json;print(json.load(open('data/secrets.json'))['setup_token'])" 2>/dev/null || $SUDO docker compose exec -T workbench python -c "import json;print(json.load(open('/data/secrets.json'))['setup_token'])")
say "Workbench is running."
echo "  1. Open        $url"
echo "  2. Setup code  $code"
echo "  3. Create your administrator account and scan the QR code with your phone."
echo
echo "Your data (database, files, backups) is in: $(pwd)/data"

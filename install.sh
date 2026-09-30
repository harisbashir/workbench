#!/usr/bin/env bash
# Workbench installer for Ubuntu / Debian servers.
#
# New server, public repository (no key needed):
#   bash <(curl -fsSL https://raw.githubusercontent.com/harisbashir/workbench/main/install.sh) \
#        https://github.com/harisbashir/workbench.git
#
# New server, private repository (sets up a read-only GitHub deploy key):
#   1. Copy this one file to the server, e.g.  scp install.sh you@server:
#   2. Run:  bash install.sh git@github.com:harisbashir/workbench.git
#
# Inside an existing copy of the repository (e.g. /opt/workbench):
#   ./install.sh               # (re)configure and start
#   ./install.sh --deploy-key  # switch updates to a deploy key
#
# Options:
#   --dir PATH        where to install (default /opt/workbench)
#   --branch NAME     follow a branch (e.g. main) instead of the newest release tag
#   --domain NAME     domain for automatic HTTPS (skips the question)
#   --no-https        don't ask about a domain; serve on http://<server>:8000
#   --deploy-key      set up or show the deploy key and point 'origin' at it
#
# Afterwards, update with:  /opt/workbench/update.sh
set -euo pipefail

REPO_URL=""
DIR="/opt/workbench"
BRANCH=""
DOMAIN_ARG=""
ASK_DOMAIN=1
ONLY_KEY=0
KEY="$HOME/.ssh/workbench_deploy_ed25519"
ALIAS="github-workbench"
# GitHub's published SSH host key fingerprint (docs.github.com → "GitHub's SSH key fingerprints")
GITHUB_ED25519="SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --domain) DOMAIN_ARG="$2"; ASK_DOMAIN=0; shift 2 ;;
    --no-https) ASK_DOMAIN=0; shift ;;
    --deploy-key) ONLY_KEY=1; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    git@*|https://*|ssh://*) REPO_URL="$1"; shift ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "Run as root or install sudo."
  SUDO="sudo"
fi

# Are we inside a copy of the repository already?
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IN_REPO=0
if [ -f "$HERE/docker-compose.yml" ] && [ -d "$HERE/.git" ]; then
  IN_REPO=1
  DIR="$HERE"
fi

# --- 1. Packages -----------------------------------------------------------------
need=()
for c in git curl ssh ssh-keygen ssh-keyscan; do command -v "$c" >/dev/null 2>&1 || need+=("$c"); done
if [ ${#need[@]} -gt 0 ]; then
  say "Installing git, curl and ssh"
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq git curl openssh-client ca-certificates >/dev/null
fi
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker"
  curl -fsSL https://get.docker.com | $SUDO sh
fi
if [ "$(id -u)" -ne 0 ] && ! id -nG | grep -qw docker; then
  # Lets this user run "docker compose …" (as in the README) without sudo after logging in again.
  # Note: members of the docker group can control the whole server, like sudo.
  $SUDO usermod -aG docker "$(id -un)" 2>/dev/null && ADDED_TO_DOCKER=1
fi
docker compose version >/dev/null 2>&1 || $SUDO docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 is required ('docker compose'). Please update Docker."
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="$SUDO docker"

# --- 2. Deploy key ---------------------------------------------------------------
github_path() {  # git@github.com:org/repo.git | https://github.com/org/repo(.git) -> org/repo.git
  printf '%s' "$1" | sed -E 's#^(git@[^:]+:|ssh://git@[^/]+/|https://[^/]+/)##; s#(\.git)?/?$#.git#'
}

setup_key() {
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  if [ ! -f "$KEY" ]; then
    say "Creating a deploy key for this server"
    ssh-keygen -q -t ed25519 -N "" -C "workbench-deploy@$(hostname)" -f "$KEY"
  fi
  chmod 600 "$KEY"
  touch "$HOME/.ssh/config" && chmod 600 "$HOME/.ssh/config"
  if ! grep -q "^Host $ALIAS\$" "$HOME/.ssh/config"; then
    cat >> "$HOME/.ssh/config" <<EOF

# Workbench updates (read-only deploy key)
Host $ALIAS
    HostName github.com
    User git
    IdentityFile $KEY
    IdentitiesOnly yes
EOF
  fi
  # Trust github.com only if its key matches the published fingerprint.
  if ! ssh-keygen -F github.com -f "$HOME/.ssh/known_hosts" >/dev/null 2>&1; then
    scanned="$(ssh-keyscan -T 15 -t ed25519 github.com 2>/dev/null || true)"
    fp="$(printf '%s\n' "$scanned" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}' || true)"
    [ -n "$fp" ] || die "Couldn't reach github.com over SSH (port 22). Allow outgoing port 22, or make the repository public and use its https:// address."
    [ "$fp" = "$GITHUB_ED25519" ] || die "github.com's SSH key fingerprint ($fp) doesn't match GitHub's published one. Check your network."
    printf '%s\n' "$scanned" >> "$HOME/.ssh/known_hosts"
  fi
  say "Add this deploy key to the GitHub repository"
  info "GitHub → your repository → Settings → Deploy keys → Add deploy key"
  info "Title: $(hostname)      Key (copy the whole line):"
  echo
  cat "$KEY.pub"
  echo
  info "Leave 'Allow write access' unticked - the server only needs to read."
  while true; do
    read -r -p "    Press Enter once the key is added (or type q to quit): " answer
    [ "$answer" = "q" ] && exit 1
    out="$(ssh -T -o BatchMode=yes "$ALIAS" 2>&1 || true)"
    if printf '%s' "$out" | grep -q "successfully authenticated"; then
      info "GitHub accepted the key."
      break
    fi
    info "Not accepted yet: $out"
  done
}

if [ $ONLY_KEY -eq 1 ]; then
  [ $IN_REPO -eq 1 ] || die "Run --deploy-key from inside the Workbench folder."
  current="$(git -C "$DIR" remote get-url origin)"
  setup_key
  git -C "$DIR" remote set-url origin "git@$ALIAS:$(github_path "$current")"
  git -C "$DIR" fetch --tags -q origin && info "Updates now use the deploy key: $(git -C "$DIR" remote get-url origin)"
  exit 0
fi

# --- 3. Get the code ---------------------------------------------------------------
# A public repository can be read over HTTPS without any key.
readable() { GIT_TERMINAL_PROMPT=0 git ls-remote -q "$1" HEAD >/dev/null 2>&1; }

if [ $IN_REPO -eq 0 ]; then
  if [ -z "$REPO_URL" ]; then
    REPO_URL="https://github.com/harisbashir/workbench.git"
  fi
  [ -n "$REPO_URL" ] || die "A repository address is needed."
  case "$REPO_URL" in
    https://*) https_url="$REPO_URL" ;;
    *) https_url="https://github.com/$(github_path "$REPO_URL")" ;;
  esac
  if readable "$https_url"; then
    clone_url="$https_url"
    info "Public repository: updates will download over HTTPS, no key needed."
  else
    info "The repository isn't public, so this server needs a deploy key."
    setup_key
    clone_url="git@$ALIAS:$(github_path "$REPO_URL")"
  fi
  if [ -d "$DIR/.git" ]; then
    info "$DIR already has Workbench; using it."
  else
    say "Downloading Workbench into $DIR"
    $SUDO mkdir -p "$DIR"
    $SUDO chown "$(id -u):$(id -g)" "$DIR"
    git clone -q "$clone_url" "$DIR"
  fi
  cd "$DIR"
  git fetch -q --tags origin
  if [ -n "$BRANCH" ]; then
    git checkout -q "$BRANCH"
  else
    latest="$(git tag -l 'v[0-9]*' --sort=-v:refname | head -n1 || true)"
    if [ -n "$latest" ]; then
      git -c advice.detachedHead=false checkout -q "$latest"
      info "Installed release $latest"
    fi
  fi
else
  cd "$DIR"
  origin="$(git remote get-url origin 2>/dev/null || true)"
  case "$origin" in
    https://github.com/*)
      if readable "$origin"; then
        info "Updates download from $origin (public, no key needed)."
      else
        say "Updates currently use $origin, which needs a sign-in"
        read -r -p "    Switch to a read-only deploy key (recommended for a server)? [Y/n] " yn
        if [ "${yn:-Y}" != "n" ] && [ "${yn:-Y}" != "N" ]; then
          setup_key
          git remote set-url origin "git@$ALIAS:$(github_path "$origin")"
        fi
      fi ;;
  esac
fi

# --- 4. Configure ---------------------------------------------------------------------
touch .env
chmod 600 .env
current_domain="$(grep -E '^WORKBENCH_DOMAIN=' .env | cut -d= -f2- || true)"
domain="$DOMAIN_ARG"
if [ $ASK_DOMAIN -eq 1 ]; then
  say "Domain name"
  info "If people reach Workbench over the internet, enter the domain pointed at this server"
  info "(e.g. workbench.yourcompany.com) to turn on HTTPS automatically."
  info "Leave empty to use http://<server-ip>:8000 (only on a VPN or office network:"
  info "Docker opens that port to every network the server is on, even with a firewall)."
  read -r -p "    Domain [${current_domain}]: " domain
  domain="${domain:-$current_domain}"
fi
current_bind="$(grep -E '^WORKBENCH_BIND=' .env | cut -d= -f2- || true)"
port="$(grep -E '^WORKBENCH_PORT=' .env | cut -d= -f2- || true)"
port="${port:-8000}"
grep -v -E '^(WORKBENCH_DOMAIN|WORKBENCH_BIND)=' .env > .env.tmp || true
mv .env.tmp .env
chmod 600 .env
profile=()
if [ -n "$domain" ]; then
  printf 'WORKBENCH_DOMAIN=%s\nWORKBENCH_BIND=127.0.0.1\n' "$domain" >> .env
  profile=(--profile https)
  url="https://$domain"
else
  # Keep an address someone chose on purpose (e.g. a VPN/Tailscale IP), but not the
  # localhost-only one that belongs to the HTTPS setup.
  if [ -n "$current_bind" ] && [ "$current_bind" != "127.0.0.1" ]; then
    printf 'WORKBENCH_BIND=%s\n' "$current_bind" >> .env
  fi
  ip="${current_bind:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  [ "$ip" = "127.0.0.1" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  url="http://${ip:-localhost}:$port"
  # Switching from HTTPS back to plain HTTP: stop the HTTPS proxy.
  $DOCKER compose --profile https stop caddy >/dev/null 2>&1 || true
fi
git config core.fileMode false   # ignore lost "executable" flags (see update.sh)
chmod +x install.sh update.sh 2>/dev/null || true

# --- 5. Start --------------------------------------------------------------------------
mkdir -p data
say "Building and starting Workbench (the first build takes a few minutes)"
$DOCKER compose "${profile[@]}" up -d --build

say "Waiting for Workbench to start"
ok=0
for _ in $(seq 1 90); do
  if $DOCKER compose exec -T workbench python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 2
done
[ $ok -eq 1 ] || die "Workbench didn't start. See: $DOCKER compose logs workbench  (run in $DIR)"

code="$($DOCKER compose exec -T workbench python -c "import json;print(json.load(open('/data/secrets.json'))['setup_token'])" 2>/dev/null || true)"
version="$(cat VERSION 2>/dev/null || echo '?')"
say "Workbench $version is running"
info "1. Open        $url"
if [ -n "$code" ]; then info "2. Setup code  $code  (only needed the first time)"; fi
info "3. Create your administrator account and scan the QR code with your phone."
if [ "${ADDED_TO_DOCKER:-0}" = 1 ]; then
  echo
  info "You were added to the 'docker' group: log out and back in once, then 'docker compose …'"
  info "commands work without sudo (until then, put sudo in front of them)."
fi
echo
info "Your data (database, files, backups) is in: $DIR/data"
info "To update later:  $DIR/update.sh"

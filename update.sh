#!/usr/bin/env bash
# Update Workbench to the newest version from GitHub.
#
#   ./update.sh              update to the newest release (or the newest commit of your branch)
#   ./update.sh --check      only say whether an update is available (exit code 10 if so)
#   ./update.sh --to v1.3.2  go to a specific version
#   ./update.sh --yes        don't ask before updating (for scheduled updates)
#
# What it does: takes a backup, downloads the new version, builds it while the
# current version keeps running, restarts, and checks that Workbench is healthy.
# If the new version doesn't start, it goes back to the previous version and
# restores the backup automatically.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CHECK=0
YES=0
TARGET=""
say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --yes|-y) YES=1; shift ;;
    --to) TARGET="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
done

[ -d .git ] || die "This folder isn't a git checkout of Workbench."
# Copies uploaded through the GitHub website or from Windows can lose the scripts'
# "executable" flag. Don't treat that as a local change, and put the flag back.
git config core.fileMode false
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
profile=()
if grep -qE '^WORKBENCH_DOMAIN=.+' .env 2>/dev/null; then profile=(--profile https); fi
compose() { $DOCKER compose "${profile[@]}" "$@"; }
LOG="data/update.log"
mkdir -p data
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

# --- What are we on, and what's newest? ---------------------------------------------
current_commit="$(git rev-parse HEAD)"
current_version="$(cat VERSION 2>/dev/null || echo '?')"
branch="$(git symbolic-ref -q --short HEAD || true)"

git fetch -q --tags --prune origin || die "Couldn't reach GitHub. Check the network (for a private repository, also the deploy key: ssh -T github-workbench)."

if [ -n "$TARGET" ]; then
  target="$TARGET"
elif [ -n "$branch" ]; then
  target="origin/$branch"
else
  target="$(git tag -l 'v[0-9]*' --sort=-v:refname | head -n1 || true)"
  [ -n "$target" ] || die "No release tags (v1.2.3) found. Use --to <tag or branch>."
fi
target_commit="$(git rev-parse --verify -q "$target^{commit}")" || die "Unknown version: $target"
target_version="$(git show "$target_commit:VERSION" 2>/dev/null || echo "$target")"

if [ "$target_commit" = "$current_commit" ]; then
  say "Workbench $current_version is up to date."
  exit 0
fi
if [ -z "$TARGET" ] && git merge-base --is-ancestor "$target_commit" "$current_commit"; then
  say "Workbench $current_version is newer than $target — nothing to do."
  exit 0
fi

say "Update available: $current_version → $target_version"
git --no-pager log --no-merges --format='    • %s' "$current_commit..$target_commit" | head -n 25 || true
[ $CHECK -eq 1 ] && exit 10

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no | sed 's/^/    /'
  die "These files were changed on the server. Move the changes elsewhere, or undo them with: git checkout -- ."
fi

if [ $YES -eq 0 ]; then
  read -r -p "    Update now? Workbench will be unavailable for about a minute. [y/N] " yn
  case "$yn" in y|Y|yes) ;; *) echo "    Cancelled."; exit 1 ;; esac
fi
log "update $current_version ($current_commit) -> $target_version ($target_commit)"

# --- 1. Backup -----------------------------------------------------------------------
backup=""
if compose ps --status running --services 2>/dev/null | grep -qx workbench; then
  say "Taking a backup"
  out="$(compose exec -T workbench manage backup_now 2>&1)" || die "The backup failed, so nothing was changed:
$out"
  backup="$(printf '%s' "$out" | grep -oE 'workbench-[0-9]{8}-[0-9]{6}\.zip' | head -n1 || true)"
  info "Saved $backup"
else
  info "Workbench isn't running, so no backup was taken first."
fi

# --- 2. New version: build while the old one keeps running --------------------------
say "Downloading $target_version"
if [ -n "$branch" ] && [ -z "$TARGET" ]; then
  git merge -q --ff-only "$target_commit" || die "Your branch has diverged from GitHub. Resolve it with git, then run this again."
else
  git -c advice.detachedHead=false checkout -q "$target_commit"
fi

say "Building"
if ! compose build; then
  log "build failed; back to $current_commit"
  git -c advice.detachedHead=false checkout -q "$current_commit" 2>/dev/null || true
  [ -n "$branch" ] && git checkout -q "$branch" && git reset -q --hard "$current_commit"
  die "The new version didn't build. Nothing was changed — Workbench $current_version is still running."
fi

say "Restarting"
compose up -d

healthy() {
  local id restarts
  for _ in $(seq 1 90); do
    if compose exec -T workbench python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    id="$(compose ps -q workbench 2>/dev/null || true)"
    restarts="$($DOCKER inspect -f '{{.RestartCount}}' "$id" 2>/dev/null || echo 0)"
    [ "${restarts:-0}" -ge 3 ] && return 1   # it keeps crashing; no point waiting
    sleep 2
  done
  return 1
}

if healthy; then
  $DOCKER image prune -f >/dev/null 2>&1 || true
  chmod +x install.sh update.sh 2>/dev/null || true
  log "ok: now $target_version"
  say "Workbench $target_version is running."
  [ -n "$backup" ] && info "The backup from before the update is data/backups/$backup"
  exit 0
fi

# --- 3. Didn't start: go back ------------------------------------------------------------
say "The new version didn't start — going back to $current_version"
compose logs --tail 40 workbench | sed 's/^/    /' || true
log "unhealthy after update; rolling back to $current_commit"
git -c advice.detachedHead=false checkout -q "$current_commit"
[ -n "$branch" ] && git checkout -q "$branch" && git reset -q --hard "$current_commit"
compose build
if [ -n "$backup" ]; then
  compose stop workbench
  compose run --rm -T workbench restore "$backup" --yes
fi
compose up -d
if healthy; then
  log "rolled back: $current_version running"
  die "Workbench $current_version is running again$( [ -n "$backup" ] && printf ', with the data from before the update restored' ). The log above shows why the update failed."
fi
die "Workbench didn't start after going back either. Check: $DOCKER compose logs workbench"

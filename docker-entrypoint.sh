#!/bin/sh
# Workbench container entrypoint.
#   serve (default)      migrate, start background jobs and the web server
#   backup               take a backup now
#   restore <file.zip>   restore a backup (stop the running container first)
#   create-admin ...     create an administrator from the command line
#   manage ...           run any Django management command
set -e

# Start as root only to fix ownership of the data folder, then drop privileges.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data
  find /data \! -user workbench -exec chown workbench:workbench {} + 2>/dev/null || true
  exec setpriv --reuid=workbench --regid=workbench --init-groups "$0" "$@"
fi

export HOME=/home/workbench
cmd="${1:-serve}"
[ $# -gt 0 ] && shift

case "$cmd" in
  serve)
    python manage.py migrate --noinput -v0
    python manage.py bootstrap
    python manage.py run_scheduler &
    exec gunicorn config.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${WORKBENCH_WORKERS:-3}" --threads 4 --worker-class gthread \
      --timeout 300 --graceful-timeout 30 \
      --access-logfile - --forwarded-allow-ips '*'
    ;;
  backup)
    exec python manage.py backup_now
    ;;
  restore)
    python manage.py restore_backup "$@"
    ;;
  create-admin)
    python manage.py migrate --noinput -v0
    exec python manage.py create_admin "$@"
    ;;
  manage)
    exec python manage.py "$@"
    ;;
  *)
    exec "$cmd" "$@"
    ;;
esac

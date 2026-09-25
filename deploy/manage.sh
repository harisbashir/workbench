#!/bin/sh
# Run a Workbench management command as the workbench user, so any files it
# creates in /data stay writable by the web server.
#   docker compose exec workbench manage backup_now
cd /app
if [ "$(id -u)" = "0" ]; then
  exec setpriv --reuid=workbench --regid=workbench --init-groups env HOME=/home/workbench python manage.py "$@"
fi
exec python manage.py "$@"

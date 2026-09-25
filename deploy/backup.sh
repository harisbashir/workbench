#!/usr/bin/env sh
# Nightly backup of the database and uploaded files. Run from the project folder, e.g. via cron:
#   15 2 * * *  cd /opt/workbench && ./deploy/backup.sh >> backups/backup.log 2>&1
# Copy the backups folder off the server (e.g. to cloud storage) and test a restore regularly.
set -eu
. ./.env
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p backups
docker compose exec -T db pg_dump -U "$WORKBENCH_DB_USER" -Fc "$WORKBENCH_DB_NAME" > "backups/db-$STAMP.dump"
docker compose run --rm -T --entrypoint "" web tar czf - -C /app media > "backups/media-$STAMP.tar.gz"
# Keep 30 days
find backups -name 'db-*.dump' -mtime +30 -delete
find backups -name 'media-*.tar.gz' -mtime +30 -delete
echo "Backup $STAMP done"

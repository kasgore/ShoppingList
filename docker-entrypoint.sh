#!/bin/sh
# On first container start (when the named volumes are empty), seed them
# from /seed/ — which is baked into the image from the seed_data/ folder
# in the repo. After that the volumes own the data, so subsequent
# redeploys preserve everything the family's added.
set -e

if [ -f /seed/shoppinglist.db ] && [ ! -f /data/shoppinglist.db ]; then
  echo "[entrypoint] seeding /data/shoppinglist.db from baked-in copy"
  cp /seed/shoppinglist.db /data/shoppinglist.db
fi

if [ -d /seed/uploads ] && [ -z "$(ls -A /app/static/uploads 2>/dev/null)" ]; then
  echo "[entrypoint] seeding /app/static/uploads from baked-in copy"
  cp -r /seed/uploads/. /app/static/uploads/ 2>/dev/null || true
fi

exec "$@"

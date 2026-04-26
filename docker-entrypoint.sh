#!/bin/sh
# Seed the named volumes from /seed/ — which is baked into the image
# from the seed_data/ folder in the repo. SEED_MODE controls when:
#
#   auto    (default)  copy only if the volume is empty / fresh
#   always              overwrite the volume from seed every start
#                       — useful while iterating; replace local DB → push
#                       → redeploy → Portainer matches local
#   never               skip seeding entirely
set -e

MODE="${SEED_MODE:-auto}"

seed_db() {
  if [ ! -f /seed/shoppinglist.db ]; then return; fi
  if [ "$MODE" = "always" ] || { [ "$MODE" = "auto" ] && [ ! -f /data/shoppinglist.db ]; }; then
    echo "[entrypoint] copying /seed/shoppinglist.db → /data/shoppinglist.db (mode=$MODE)"
    cp /seed/shoppinglist.db /data/shoppinglist.db
  fi
}

seed_uploads() {
  if [ ! -d /seed/uploads ]; then return; fi
  if [ "$MODE" = "always" ]; then
    echo "[entrypoint] overwriting /app/static/uploads from /seed/uploads (mode=always)"
    rm -rf /app/static/uploads/*
    cp -r /seed/uploads/. /app/static/uploads/ 2>/dev/null || true
  elif [ "$MODE" = "auto" ] && [ -z "$(ls -A /app/static/uploads 2>/dev/null)" ]; then
    echo "[entrypoint] seeding /app/static/uploads from /seed/uploads (mode=auto, empty)"
    cp -r /seed/uploads/. /app/static/uploads/ 2>/dev/null || true
  fi
}

if [ "$MODE" != "never" ]; then
  seed_db
  seed_uploads
else
  echo "[entrypoint] SEED_MODE=never — leaving volumes alone"
fi

exec "$@"

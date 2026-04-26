"""Snapshot the local dev DB + uploaded photos into seed_data/, ready to
commit and ship to Docker.

When the Docker container starts on a host whose volumes are still empty,
it copies seed_data/ into them — so the freshly deployed app has the same
recipes and photos you've been using locally.

Usage:
    python finalize.py              # copy current state into seed_data/
    python finalize.py --clear      # remove seed_data/ contents (revert to
                                    # default-seeded recipes on next deploy)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_DB = os.path.join(ROOT, "shoppinglist.db")
LOCAL_UPLOADS = os.path.join(ROOT, "static", "uploads")
SEED_DIR = os.path.join(ROOT, "seed_data")
SEED_DB = os.path.join(SEED_DIR, "shoppinglist.db")
SEED_UPLOADS = os.path.join(SEED_DIR, "uploads")


def clear_seed() -> None:
    if not os.path.exists(SEED_DIR):
        print("seed_data/ doesn't exist; nothing to clear")
        return
    for name in os.listdir(SEED_DIR):
        if name == ".gitkeep":
            continue
        path = os.path.join(SEED_DIR, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    print("cleared seed_data/")


def snapshot() -> None:
    if not os.path.exists(LOCAL_DB):
        print(f"ERROR: {LOCAL_DB} doesn't exist — run the dev app at least once "
              "(python app.py) so the DB is created.", file=sys.stderr)
        sys.exit(1)
    os.makedirs(SEED_DIR, exist_ok=True)

    # DB
    shutil.copy2(LOCAL_DB, SEED_DB)
    size_kb = os.path.getsize(SEED_DB) / 1024
    print(f"snapshotted shoppinglist.db ({size_kb:.0f} KB)")

    # Uploads
    if os.path.exists(SEED_UPLOADS):
        shutil.rmtree(SEED_UPLOADS)
    if os.path.isdir(LOCAL_UPLOADS):
        files = [f for f in os.listdir(LOCAL_UPLOADS) if not f.startswith(".")]
        if files:
            shutil.copytree(LOCAL_UPLOADS, SEED_UPLOADS)
            print(f"snapshotted {len(files)} upload(s)")
        else:
            print("no uploads to snapshot")
    else:
        print("no uploads directory")

    print()
    print("Next steps:")
    print('  git add seed_data')
    print('  git commit -m "Update deploy seed snapshot"')
    print("  git push")
    print()
    print("Then in Portainer: remove the stack + the named volumes")
    print("(shoppinglist-data, shoppinglist-uploads), and redeploy. The")
    print("entrypoint will copy seed_data/ into the empty volumes on first run.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear", action="store_true",
                        help="empty seed_data/ instead of snapshotting")
    args = parser.parse_args()
    if args.clear:
        clear_seed()
    else:
        snapshot()

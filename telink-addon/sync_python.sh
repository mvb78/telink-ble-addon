#!/usr/bin/env bash
# Re-sync the Python package into the self-contained add-on build directory.
# Run this from the repository root after editing any *.py module:
#   ./telink-addon/sync_python.sh
set -euo pipefail

SRC="."
DST="telink-addon/python"

FILES=(config.py lamp_registry.py group_registry.py telink_cli.py
       telink_daemon.py telink_crypto.py telink_mesh.py telink_ble.py web_app.py)

mkdir -p "$DST/templates" "$DST/static"
for f in "${FILES[@]}"; do
  cp "$SRC/$f" "$DST/$f"
done
cp templates/*.html "$DST/templates/"
cp -r static/. "$DST/static/"

echo "Synced ${#FILES[@]} modules + web assets into $DST"

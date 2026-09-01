#!/usr/bin/env bash
# Deploy/restart the privileged Telink BLE sidecar daemon on HAOS.
#
# The sidecar owns the BLE connections (privileged => raw HCI monitor works,
# which the add-on's own container can't open). It shares the add-on's /data
# and the host D-Bus, and listens on TCP 0.0.0.0:8097 for the add-on to bridge.
#
# Usage:  sudo ./deploy_sidecar.sh [image-tag]      (default: 1.1.0)
set -euo pipefail

TAG="${1:-1.1.0}"
IMAGE="ghcr.io/mvb78/telink-ble-cli:${TAG}"
NAME="telink-daemon"
ADDON_CT="app_c4c18bb5_telink_ble_cli"

# Find the add-on's /data host path so both processes share the registry.
DATA_DIR="$(docker inspect "$ADDON_CT" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)"
if [ -z "$DATA_DIR" ]; then
  echo "ERROR: add-on container '$ADDON_CT' not found or /data mount missing." >&2
  exit 1
fi
echo "Using add-on /data at: $DATA_DIR"

# Graceful stop (SIGTERM -> clean BLE disconnect), never force-kill.
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "Stopping old sidecar gracefully ..."
  docker stop -t 15 "$NAME"
  docker rm "$NAME" >/dev/null
fi

echo "Starting sidecar $IMAGE ..."
docker run -d --name "$NAME" \
  --privileged \
  --network host \
  --restart unless-stopped \
  -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
  -v "$DATA_DIR":/data \
  -e TELINK_DATA_DIR=/data \
  -e TELINK_KNOWN_PASSWORDS=8888 \
  -e TELINK_DAEMON_HOST=0.0.0.0 \
  -e TELINK_DAEMON_PORT=8097 \
  "$IMAGE" \
  python3 telink_daemon.py

echo "Sidecar started. Logs:"
sleep 3
docker logs "$NAME" --tail 5
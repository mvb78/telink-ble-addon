#!/usr/bin/env bash
# Sidecar entrypoint (Variant B): run ONLY the BLE daemon in a privileged
# container that listens on TCP so the Supervisor add-on can bridge to it.
# The raw HCI monitor needs the seccomp-free privileged mode this container
# provides; the add-on's own container blocks socket(AF_BLUETOOTH).
set -e

export TELINK_DATA_DIR="${TELINK_DATA_DIR:-/data}"
export TELINK_DAEMON_HOST="${TELINK_DAEMON_HOST:-0.0.0.0}"
export TELINK_DAEMON_PORT="${TELINK_DAEMON_PORT:-8097}"

mkdir -p "$TELINK_DATA_DIR"

exec python3 telink_daemon.py
#!/usr/bin/env bash
# Home Assistant add-on entrypoint for the telink-ble-cli web UI + daemon.
#
# Responsibilities:
#   * Ensure the persistent /data dir exists (Supervisor keeps it across reboots).
#   * Optionally grant the raw-HCI capability if not already running privileged
#     as root (harmless no-op otherwise).
#   * Start the telink daemon as a monitored background child.
#   * Serve the Flask app in the foreground with waitress (production WSGI).
#   * Forward SIGTERM/SIGSTOP to both processes and exit cleanly (S6 protocol).

set -e

export TELINK_DATA_DIR="${TELINK_DATA_DIR:-/data}"
export TELINK_WEB_HOST="${TELINK_WEB_HOST:-0.0.0.0}"
export TELINK_WEB_PORT="${TELINK_WEB_PORT:-8099}"
export TELINK_WEB_WSGI=1
export TELINK_WEB_DEBUG=0

# Supervisor injects the add-on config (options/schema in config.yaml) as
# CONFIG_<OPTION> env vars; the python package reads TELINK_* instead. Map them.
[ -n "${CONFIG_KNOWN_PASSWORDS:-}" ] && export TELINK_KNOWN_PASSWORDS="${CONFIG_KNOWN_PASSWORDS}"
[ -n "${CONFIG_SCAN_TIMEOUT:-}" ] && export TELINK_SCAN_TIMEOUT="${CONFIG_SCAN_TIMEOUT}"

mkdir -p "$TELINK_DATA_DIR"

# The raw HCI_CHANNEL_MONITOR socket (used for ATT notify capture) needs
# CAP_NET_ADMIN. As root with privileged NET_ADMIN we already have it, so this
# only matters for unprivileged runs — belt and braces, never fatal.
PYBIN="$(command -v python3)"
if [ -x "$PYBIN" ] && command -v setcap >/dev/null 2>&1; then
  setcap cap_net_admin,cap_net_raw+eip "$PYBIN" 2>/dev/null || true
fi

DAEMON_PID=""

PAUSED="$TELINK_DATA_DIR/daemon_paused"

start_daemon() {
  if [ -f "$PAUSED" ]; then
    echo "[run] daemon paused (flag present) — not starting"
    return 0
  fi
  echo "[run] starting daemon"
  python3 telink_daemon.py &
  DAEMON_PID=$!
  echo "[run] daemon pid $DAEMON_PID"
}

stop_all() {
  echo "[run] stopping (sig=$1)"
  if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -TERM "$DAEMON_PID" 2>/dev/null || true
  fi
  # Wait briefly for the daemon to release its BLE connections.
  for _ in $(seq 1 10); do
    [ -z "$DAEMON_PID" ] && break
    kill -0 "$DAEMON_PID" 2>/dev/null || break
    sleep 0.5
  done
  exit 0
}

trap 'stop_all TERM' SIGTERM
trap 'stop_all INT' SIGINT

start_daemon

echo "[run] starting web UI on ${TELINK_WEB_HOST}:${TELINK_WEB_PORT}"
# Foreground; waitress handles SIGTERM for the web process via default handlers.
TELINK_WEB_DEBUG=0 python3 web_app.py &
WEB_PID=$!
trap 'kill -TERM "$WEB_PID" 2>/dev/null; stop_all TERM' SIGTERM

# Hold the foreground slot; propagate TERM to both children.
while true; do
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    echo "[run] web process exited unexpectedly"
    stop_all TERM
  fi
  [ -n "$DAEMON_PID" ] && ! kill -0 "$DAEMON_PID" 2>/dev/null && \
    { [ -f "$PAUSED" ] && echo "[run] daemon stopped and paused — holding" || \
      { echo "[run] daemon exited; restarting in 5s" && sleep 5 && start_daemon; }; }
  sleep 2
done

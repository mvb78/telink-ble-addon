# Home Assistant add-on for telink-ble-cli.
#
# BLE strategy: the add-on talks to the host's Bluetooth stack via D-Bus
# (bleak -> BlueZ -> host adapter/dongle). host_dbus: true in config.yaml
# mounts the host D-Bus system socket so the container can drive BlueZ and
# open a raw HCI_CHANNEL_MONITOR socket (used by telink_ble.py to capture ATT
# NOTIFY packets). Privileged NET_ADMIN + NET_RAW are required for that raw
# HCI socket; run.sh drops capability-setcap boilerplate since the container
# runs as root with those caps granted by the Supervisor.
FROM python:3.12-slim

# Supervisor 2026.04+ no longer injects these via the legacy builder; keep them
# here so locally built images carry the io.hass labels the Supervisor expects.
LABEL \
  io.hass.version="1.0.45" \
  io.hass.type="addon" \
  io.hass.arch="amd64" \
  org.opencontainers.image.title="Telink BLE CLI" \
  org.opencontainers.image.description="Web UI + Home Assistant bridge for Telink BLE mesh lamps" \
  org.opencontainers.image.source="https://github.com/mvb78/telink-ble-addon"

# libcap2-bin provides setcap as a fallback if the add-on ever runs unprivileged;
# procps so run.sh can monitor the daemon process without pgrep races.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libcap2-bin \
        procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python package + web assets
COPY python/ ./python/
COPY requirements.txt ./requirements.txt

# Then install deps (separate layer so code edits don't re-resolve pip).
RUN pip install --no-cache-dir -r requirements.txt

COPY run.sh /run.sh
COPY run_daemon.sh /run_daemon.sh
RUN chmod +x /run.sh /run_daemon.sh

# -- S6-style signal handling -------------------------------------------------
# HA Supervisor expects the add-on to stay in the foreground and handle
# SIGTERM/SIGSTOP. run.sh runs the web server in the foreground (waitress) and
# the daemon as a supervised background child; both propagate SHUT.
ENV TELINK_DATA_DIR=/data
ENV TELINK_WEB_HOST=0.0.0.0
ENV TELINK_WEB_PORT=8099

EXPOSE 8099
WORKDIR /app/python

CMD ["/run.sh"]

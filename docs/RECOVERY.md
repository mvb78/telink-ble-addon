# Recovery & Troubleshooting

Quick reference for the recurring issues seen on this install. Everything here
is also captured (with more context) in `progress.md`.

## 1. Lamps won't respond / aren't advertising

Symptom: the web UI shows "offline", the HA entities go `unknown`, and a scan
finds no Telink devices. Telink lamps **stop advertising while connected**, so
a held session strands them in a silent state.

Recovery:
1. Check the sidecar log for idle-release / reconnect activity:
   ```bash
   docker logs telink-daemon --tail 30
   ```
2. If sessions are held, the idle-release (default 120 s) will drop them; wait
   ~2 minutes and rescan.
3. If they still don't advertise: **power-cycle the lamps at the wall**
   (off ~10 s, back ON — leave them on/idle). They boot into the advertising
   state.

The daemon reconnects automatically (exact-MAC sessions, watcher retries every
15 s). No further action needed once they're advertising.

## 2. Add-on / sidecar restart (always graceful)

Never force-kill the sidecar — SIGKILL drops the BLE connections abruptly and
can strand the lamps.

```bash
# graceful stop -> clean disconnect, then remove
docker stop -t 15 telink-daemon
docker rm telink-daemon
# redeploy (see docs/INSTALL_HAOS.md for the full run command)
```

## 3. Supervisor wedged (updates blocked, reboots fail, "freeze")

Symptom: add-on updates fail with "no host internet connection" or "system is
not running - freeze"; `ha supervisor restart` and `ha host reboot` hang or are
blocked; `/supervisor/info` times out.

Recovery:
- First confirm it's not just a missing token: export it before using `ha`:
  ```bash
  export SUPERVISOR_TOKEN=$(sudo docker inspect app_a0d7b954_ssh \
    --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^SUPERVISOR_TOKEN=' | cut -d= -f2)
  ```
- A stuck "freeze" flag blocks backups and supervisor restarts. Software
  reboots (`ha host reboot`, busybox `reboot`) often fail while wedged.
- **Only a physical power-cycle of the NUC reliably clears it.** Power off
  ~15 s, back on, wait ~3 min.

## 4. Add-on web UI dead (waitress queue saturation)

Symptom: port 8098 returns `000`; log shows `waitress.queue: Task queue depth`
climbing. Caused by requests hanging (usually direct-connect fallback when the
sidecar is unreachable).

Recovery:
- Restart the add-on container (1.0.46+ starts its server before connecting,
  so this should be rare):
  ```bash
  sudo docker restart app_c4c18bb5_telink_ble_cli
  ```
- The add-on has a `watchdog` since 1.1.0, so the Supervisor should restart it
  automatically now.

## 5. Host internet check wrong (`host_internet: false`)

Symptom: add-on updates/installs blocked with "no host internet connection"
even though everything works.

Recovery: clear the cached supervisor state — usually resolved by a physical
NUC reboot (see #3). The supervisor itself reports `supervisor_internet: true`.

## 6. HA integration: per-lamp entities stay `unknown`

The add-on's status response must include the lamp MAC (fixed in 1.0.47).
If entities are unknown:
- Verify the add-on is on **1.0.47+** (1.1.0).
- Check the integration's poll: `POST /api/command/status` should return
  entries with a `mac` field.

## 7. Color temperature

The lamps are tunable-white only, **2700–6500 K** (0–100 on the add-on scale).
2000 K is below the range and is clamped to 2700 K (warmest). "Full warm white"
= 2700 K; "90 % warm" ≈ 3080 K.
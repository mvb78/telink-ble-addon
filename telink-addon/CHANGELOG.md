# Changelog

## 1.0.27 - 2026-09-01
- Add `8888` to the default `known_passwords` list (config default + code
  fallback). The Smart_qXsx lamps use the mesh password `8888`, so discovery
  would silently fail to log in and save lamps with only the old defaults.

## 1.0.26 - 2026-08-31
- Move add-on host port mapping from 8099 to 8098: host port 8099 is taken
  by a `ttyd` process on this installation, so the mapping silently failed
  and the HA integration could not reach the add-on. Requires image rebuild
  (version tag) so the container is recreated with the new mapping.

## 1.0.25 - 2026-08-31
- Switch to **prebuilt image distribution**: `image: ghcr.io/mvb78/telink-ble-cli`
  in config.yaml; the store install now pulls the image instead of running a
  local docker buildx build (which silently hangs on Supervisor 7.x/HAOS 6.1).
- Dockerfile: add `io.hass.*` + OCI labels per the 2026 builder-migration docs.

## 1.0.22 - 2026-08-30
- Fix add-on `url:` so the store "Visit ... page" link points to the **public**
  repo (`https://github.com/mvb78/telink-ble-addon`) instead of the private one.

## 1.0.21 - 2026-08-30
- Fix group commands (`dst=<group>`) by selecting a **working relay lamp** that is
  currently provisioned in the mesh (current `Smart_mesh`/`8888` creds) instead of
  blindly using the first lamp in the registry (which could be a broken/out-of-mesh
  lamp). This makes `light.telink_oben` / `light.telink_arbeitsplatte` group control
  work end to end.

## 1.0.20 - 2026-08-30
- Pick a working relay lamp for group-destination (`dst`) commands.

## 1.0.19 - 2026-08-30
- Faster brute-force login harness (0.12s delay between attempts).

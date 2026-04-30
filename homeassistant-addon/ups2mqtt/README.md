# Home Assistant Community App Path

This directory contains Home Assistant Community App/add-on packaging for `ups2mqtt`.

## Highlights

- Ingress-enabled (`ingress: true`, `ingress_port: 8099`)
- Optional direct port mapping (`8099/tcp`) for troubleshooting
- Uses add-on options as configuration source (`/data/options.json`)
- Persists runtime state in `/data`
- HTMX-only web UI surface (legacy non-HTMX page/action routes removed)

## Local structure

- `config.yaml`: add-on metadata/options/schema
- `Dockerfile`: add-on image build
- `app/`: bundled ups2mqtt runtime code copied for add-on build context
- `rootfs/etc/cont-init.d/10-ups2mqtt-options`: prepares runtime options
- `rootfs/etc/services.d/ups2mqtt/run`: starts runtime with ingress-aware env

## Runtime Tree Sync

This repo currently keeps two runtime trees in sync:

- canonical source: `homeassistant-addon/ups2mqtt/app/ups2mqtt/`
- mirrored copy: `ups2mqtt/rootfs/usr/src/app/ups2mqtt/`

Use:

- `make runtime-check` to verify they match
- `make runtime-sync` to update the mirror from the canonical source
- `make release-check` before tagging/releasing (fails if trees drift)

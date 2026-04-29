# Home Assistant Community App Path

This directory contains Home Assistant Community App/add-on packaging for `ups2mqtt`.

## Highlights

- Ingress-enabled (`ingress: true`, `ingress_port: 8099`)
- Optional direct port mapping (`8099/tcp`) for troubleshooting
- Uses add-on options as configuration source (`/data/options.json`)
- Persists runtime state in `/data`

## Local structure

- `config.yaml`: add-on metadata/options/schema
- `build.yaml`: architecture build mapping
- `Dockerfile`: add-on image build
- `rootfs/etc/cont-init.d/10-ups2mqtt-options`: prepares runtime options
- `rootfs/etc/services.d/ups2mqtt/run`: starts runtime with ingress-aware env

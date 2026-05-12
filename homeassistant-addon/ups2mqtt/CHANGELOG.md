# Changelog

All notable changes to the Home Assistant add-on are documented in this file.

## v1.2.9

- Patched Dependabot-flagged transitive security dependencies in runtime lockfiles:
  - `urllib3` `2.6.3` -> `2.7.0`
  - `python-multipart` `0.0.26` -> `0.0.28`
- Updated canonical and mirrored runtime pytest expectations for current HTMX/UI rendering and device validation behavior.
- Restored full pytest suite pass in both canonical and runtime trees.

## v1.2.8

- Added hover-help (`ⓘ`) hints across admin HTMX surfaces:
  - Devices table headers and row actions.
  - Configuration panel labels and runtime timer layout refinements.
  - Maintenance panel buttons.
  - Metrics panel headers and action buttons.
  - Profiles panel/table actions and profile form actions.
  - Profile Builder panel buttons and discovered-capabilities table headers.
- Enabled Home Assistant bridge visibility by default while preserving runtime/env overrides.

## v1.2.7

- Decommissioned obsolete adaptive-concurrency runtime/config surfaces.
- Set runtime log-level default fallback to `ERROR` while preserving explicit overrides.
- Standardized default `max_concurrent_polls` to `10`.
- Added preferred metrics key `backpressure.concurrency_limiter` and kept `backpressure.adaptive_concurrency` as a compatibility alias.
- Removed obsolete standalone adaptive-concurrency environment passthroughs.

## v1.2.6

- Expanded protocol and polling regression coverage across NUT/APCUPSD/Modbus paths.
- Hardened device/profile edit and payload handling across HTMX device workflows.
- Synced documentation and release metadata for the 1.2.6 release.

## v1.2.5

- Modbus optimizations release.
- Enforced selected-sensor-only Modbus poll-plan generation before dispatch across:
  - CyberPower
  - APC SMT
  - APC Smart
  - APC PDU
- Prevented non-selected descriptor/block reads from being scheduled in minimal profiles.
- Added focused poll-plan regression tests for minimal vs explicitly enabled optional sensor paths.

## v1.2.4

- Fixed Home Assistant add-on direct Prometheus access gap.
- Added optional direct metrics port mapping `8100/tcp` for metrics-only scraping.
- Added add-on `metrics_port` option (default `8100`) wired to runtime `UPS2MQTT_METRICS_PORT`.
- Updated add-on docs for direct scrape paths: `/metrics` and `/metrics/prometheus`.

## v1.2.3

- Added profile-only maintenance restore (`Restore Profiles from JSON`) that restores reusable profiles without importing devices/settings.
- Updated `Remove All Profiles` to localize global-profile devices first (copy effective profile payload, selected sensors, and sensor preferences into device-local overrides) before deleting reusable profiles.
- Fixed device poll-interval handling in add/edit flows:
  - Defaults from profile `poll_groups.fast` where available.
  - Preserves custom user values.
  - Rejects blank/invalid interval values with modal-safe validation.
- Added focused regression coverage for maintenance restore/remove-all and device modal poll-interval behavior.

## v1.2.2

- CSV import/export and device edit-field fixes.
- Port handling improvements for NUT and APCUPSD workflows.

## v1.2.1

- Reintroduced CSV export action in Maintenance/CSV onboarding flow.

## v1.2.0

- Added optional Prometheus telemetry scrape endpoint support.
- Added optional InfluxDB v3 telemetry export support.
- Kept MQTT/Home Assistant as primary path; telemetry exporters remain optional.

## v1.1.2

- Endpoint-locking and profile sensor-control fixes.
- Formatting/lint cleanup release.

## v1.1.0

- Consolidated release including profile/runtime fixes and quality updates from prior maintenance work.

## v1.0.3

- NUT profile builder updates.
- Runtime passthrough improvements for selected raw keys.
- Home Assistant-safe discovery key normalization.

## v1.0.2

- Versioning/admin follow-up release.

## v1.0.1

- Admin/maintenance follow-up release.

## v1.0.0

- First stable `1.x` release.

## v0.90.0-ha2 ... v0.90.0-ha27

- HA add-on hardening and ingress fixes.
- Migration to HTMX-only web surface and removal of legacy non-HTMX routes.
- Polling fairness/adaptive concurrency iterations and related metrics visibility.
- SNMP polling/performance updates and SNMP port/unit-id handling fixes.
- CSV/JSON maintenance flow updates and remove-all maintenance actions.

## v0.90.0

- Compatibility documentation and release baseline for the 0.90 series.

## v0.1.0rc5

- Early release-candidate baseline for the standalone-to-add-on evolution.

---

## Standalone Repository Notes (`../ups2mqtt-standalone`)

The sibling standalone repository currently does not publish SemVer release tags or a dedicated changelog file.  
For traceability, the most recent documented standalone milestones are:

- `82539e1` - Document current standalone status and operational notes.
- `f55e58d` - Fix profile schema migration and update development docs.
- `ac9eabd` - Switch startup UI route to `/htmx/devices`.
- `632e7c1` - Bump `uv` dependencies and document lint workflow.
- `195c579` - Initial standalone `ups2mqtt` repository.

Key standalone scope notes (from `../ups2mqtt-standalone/README.md`):

- Deployment scope is intentionally trimmed to Docker Compose runtime + app runtime code.
- Startup UI path: `http://localhost:8099/htmx/devices`.
- Metrics/security posture: web UI is unauthenticated and should not be exposed to untrusted networks.
- Driver coverage summary includes APC Smart UPS (legacy Modbus/SNMP), APC SMT, CyberPower Modbus, RFC1628 UPS-MIB expectations, and limited APC PDU support.

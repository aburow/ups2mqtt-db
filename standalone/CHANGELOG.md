## 2026-05-07

### ups2mqtt-db

- cd2cf7c release: v1.2.4 prometheus direct access fix
- a1465c2 release: v1.2.3 profile restore and poll-interval fixes

———

## 2026-05-06

### ups2mqtt-db

- b3489d0 release v1.2.2: minor CSV and device-edit field fixes
- 24af246 release v1.2.1: re-introduce maintenance CSV export
- c916c0d docs: update compatibility and telemetry sections in README
- 261e43d release v1.2.0: add optional Prometheus and InfluxDB v3 telemetry
- 7d55ca5 release: v1.1.2
- 0c33ca6 format ups2mqtt python files
- 84d08ce fix endpoint locking and profile sensor controls
- 3349299 release: v1.1.0

———

## 2026-05-05

### ups2mqtt-db

- 0f303db Release v1.0.3: NUT profile builder, runtime passthrough, and HA-safe discovery key normalization

### nut-interface

- snapshot: local-only source (no git repository metadata available)
- snapshot: tested against local NUT build `2.8.5.323-323+ga630434f9` (post-2.8.5 dev iteration)
- snapshot: STARTTLS support present
- snapshot: multi-device NUT server scenario support present
- snapshot: evidence files include `nutpoller.py`, `AGENTS.md` (updated in this window)

### apcupsd-interface

- snapshot: local-only source (no git repository metadata available)
- snapshot: tested against `apcupsd 3.14.14 (31 May 2016) debian`
- snapshot: evidence files include `apcupsd_nis.py`, `list_datapoints.py` (updated in this window)

———

## 2026-05-04

### ups2mqtt-db

- 1bf7386 admin tasks for versioning
- 4e9a3d8 admin tasks
- 3ca668d version bump
- a7752d8 release: 1.0.0
- 43a1376 Release ups2mqtt HA add-on 0.90.0-ha27
- 8e0e074 Batch SNMP fallback polling and clear metrics errors
- 6c6a989 Release ups2mqtt HA add-on 0.90.0-ha26
- 7274774 Improve polling cadence and metrics observability

———

## 2026-05-03

### ups2mqtt-db

- c03c597 Add timing load averages to metrics UI and tune adaptive concurrency defaults
- 842a956 Fix SNMP unit_id save regression, restore legacy route aliases, release ha25
- ed6e7ec Allow SNMP unit_id=0 in web validation and release ha24
- bc5baf0 Back off adaptive concurrency on timeouts

### ups-sim

- 3f34b7e add ups simulator conductor api
- f822583 Use ups-sim entrypoint for simulator start/stop
- 2dddf76 Apply latency profiles to SNMP and restart path
- e4fddbf Add real-device latency profiling and replay support
- b3cc343 Stop tracking generated runlogs
- 60b45ca Initial ups-sim import

———

## 2026-05-02

### ups2mqtt-db

- bf41ec2 Fix metrics accounting after resets
- c3150aa Fix HA direct port and fair source scheduling

———

## 2026-04-25

### ups2mqtt-standalone

- 82539e1 Document current standalone status and operational notes
- f55e58d Fix profile schema migration and update dev docs
- ac9eabd Switch startup UI route to /htmx/devices
- 632e7c1 Bump uv dependencies and document lint workflow
- 195c579 Initial standalone ups2mqtt repository

———

## 2026-04-20

### apc-modbus-ha

- af3f58f chore(release): v1.2.3-dev.20

### ups-snmp-ha

- 3ac5904 release: 1.1.1-dev8

### cyberpower-modbus-ha

- 8965a4f release: 1.1.1-dev.8

———

## 2026-04-18

### apc-modbus-ha

- 9c3ae4d release: v1.2.3-dev.19
- 4d0b8b4 release: v1.2.3-dev.18

———

## 2026-04-17

### apc-modbus-ha

- 6feb694 Gate SNMP probe polling behind hourly detection; bump 1.2.3-dev.17
- dca1b84 Add keep-connection-open switch and bump 1.2.3-dev.16
- 971b61d Promote poll timing breakdown to info and bump 1.2.3-dev.15
- 0e94b9c Bump prerelease to 1.2.3-dev.14
- 83552e2 Add poll timing instrumentation and fleet scan guard

### ups-snmp-ha

- a4100ad Add SNMP poll timing instrumentation and bump 1.1.1-dev7

### cyberpower-modbus-ha

- f33b6ff release: prepare v1.1.1-dev.7 prerelease

———

## 2026-04-14

### ups2mqtt-framework

- e98c4cc Bootstrap ups2mqtt-framework with contracts, lint pipeline, and isolated CodeQL

### apc-modbus-ha

- 724552b Bump prerelease to 1.2.3-dev.13
- eb33406 Add AP9640 input-frequency SNMP fallback and diagnostics

———

## 2026-04-13

### apc-modbus-ha

- 4462231 Fix unified smart/smt mapping drift and bump 1.2.3-dev.12
- 943024a Align rack PDU unified defaults and bump 1.2.3-dev.11
- cdffa98 Fix bridge default enablement for rack PDU and bump 1.2.3-dev.10
- 4764bd7 Add rack PDU default monitor profile and bump 1.2.3-dev.9
- 0653de8 Add UPS unified interop contract profiles and bump 1.2.3-dev.8

### ups-snmp-ha

- 82686d8 Prepare v1.1.1-dev6 prerelease

### cyberpower-modbus-ha

- 8e8e6a0 release: prepare v1.1.1-dev.6 prerelease

———

## 2026-04-12

### apc-modbus-ha

- 0223e05 Inject coordinator metadata for bridge and bump 1.2.3-dev.7
- 11c6035 Add unified device info export contract and bump 1.2.3-dev.6
- 80f7464 Add reset monitor defaults button and bump 1.2.3-dev.5

### ups-snmp-ha

- 5fa6c45 Prepare v1.1.1-dev5 prerelease
- 56813e7 Add reset monitors button/service and prepare v1.1.1-dev4

### cyberpower-modbus-ha

- 57f6871 release: prepare v1.1.1-dev.5 prerelease
- 155cb42 release: prepare v1.1.1-dev.4 prerelease

———

## 2026-04-11

### apc-modbus-ha

- 6dd1d56 Keep full block polling and bump 1.2.3-dev.4
- 96499ba Fix core block schema and bump 1.2.3-dev.3
- b1c7489 Add core-first availability and bump 1.2.3-dev.2
- b371a1b Adopt unified icon mappings and bump 1.2.3-dev.1
- 87efe5a Add explicit entity icons and bump 1.2.2-dev.7
- 3f30b03 Merge branch 'v1.2.2-dev' for release 1.2.2
- a0e15e7 Release 1.2.2

### ups-snmp-ha

- 2c854af Prepare v1.1.1-dev3 prerelease
- 1c9338a Prepare v1.1.1-dev2 prerelease
- 182ab59 Prepare v1.1.1-dev1 prerelease
- b5e9ea5 Prepare v1.1.1-dev prerelease

### cyberpower-modbus-ha

- 318d585 release: prepare v1.1.1-dev.2 prerelease
- f76faf7 refactor: rename icons.py to icons_unified.py for consistency
- fba229a release: prepare v1.1.1-dev.1 prerelease
- 2705aef merge: release v1.1.0 from v1.1.0-dev
- 3c87dad merge: include final v1.1.0 branch commits
- 689b186 release: prepare v1.1.0-dev.1 prerelease
- cae5248 release: finalize v1.1.0 metadata

———

## 2026-04-10

### apc-modbus-ha

- d74e846 Promote cycle boundary logs to info and bump 1.2.2-dev.6

———

## 2026-04-09

### apc-modbus-ha

- ba2d79e Gate startup re-detect and bump 1.2.2-dev.5
- a039b0a Add startup staggering and bump 1.2.2-dev.4

———

## 2026-04-08

### apc-modbus-ha

- 8c97b95 Use family-aware device info fallback and bump 1.2.2-dev.3
- 3b9e616 Align collector probes with detection and bump 1.2.2-dev.2
- 94b85cb Improve probe-pattern detection and bump 1.2.2-dev.1

———

## 2026-04-05

### apc-modbus-ha

- b1de6e2 Keep OID fields visible in sanitized diagnostics output
- 073593b Release 1.2.1
- b972a1e Merge branch 'v1.2.0-dev' for release 1.2.0
- 3060005 Release 1.2.0

———

## 2026-04-04

### apc-modbus-ha

- d133472 Hide optional probe entities when no values and bump 1.2.0-dev.4

———

## 2026-04-03

### apc-modbus-ha

- 5b11631 Fix SNMP probe indexing/scaling and bump 1.2.0-dev.3

———

## 2026-04-02

### apc-modbus-ha

- 815d80f Add manual diagnostics button and bump 1.2.0-dev.2
- 4dc66f1 Add SNMP-only external probe sensors and bump 1.2.0-dev.1

———

## 2026-04-01

### apc-modbus-ha

- 7a5dc2d Clarify supported device families in README intro
- 48c8043 Update README compatibility notes and ignore local agent metadata

———

## 2026-03-28

### apc-modbus-ha

- e60a2b2 Release v1.1.0
- c5fa088 Prepare stable 1.1.0 release metadata

———

## 2026-03-06

### cyberpower-modbus-ha

- 8356eea Fix grain lint findings

———

## 2026-02-18

### ups-snmp-ha

- e87ba19 Release 1.0.3
- 8806896 Update docs for 1.0.3 prerelease behavior and debugging
- 5aa92bd Support UPS-MIB output_load index 0 fallback and bump to 1.0.3-dev2
- 75af967 Handle missing OIDs and start 1.0.3-dev1 prerelease series
- b7a1285 Poll output load on fast interval and bump to 1.0.2
- 8cd5938 Bump integration version to 1.0.1
- 6ca227a Add output load sensor support for UPS-MIB and APC

———

## 2026-02-17

### eversolar-pmu-ha

- f3b7bf2 docs: make README mermaid flow vertical

———

## 2026-02-09

### ups-snmp-ha

- dd8a6da Release 1.0.0

### cyberpower-modbus-ha

- 0a8982d Release 1.0.0

———

## 2026-02-07

### ups-snmp-ha

- 1f58593 docs: use br in mermaid labels
- 269684e docs: convert architecture diagram to mermaid

### cyberpower-modbus-ha

- c58222e Fix Mermaid line breaks
- 5abfb15 Update README diagram and HACS badge
- 22fd8cf Move brand assets into custom component

### eversolar-pmu-ha

- 1de37d8 Add architecture diagram
- d75ac94 Add PR template and brand assets

———

## 2026-01-30

### ups-snmp-ha

- c63315b rename workflow files
- 43f1e32 Resize logo and icon assets

### cyberpower-modbus-ha

- e9056f4 Delete validate.yml
- 5d4eb1d Create hacs.yaml

### eversolar-pmu-ha

- 571c676 Bump version to 1.2.1 - versioning alignment

———

## 2026-01-29

### cyberpower-modbus-ha

- 2a4f8e8 Release 0.4.0

### ups-snmp-ha

- f1e3caa Fast-poll and battery/status behavior update
- 3f0b447 Fast-poll and battery/status behavior update
- 0256aae Fast-poll and battery/status behavior update
- d04b7ed Fast-poll and battery/status behavior update
- 25dfebe Fast-poll and battery/status behavior update
- 032f7eb Fast-poll and battery/status behavior update

### eversolar-pmu-ha

- d5f4864 HACS/workflow/manifest stabilization update
- 8dbb755 HACS/workflow/manifest stabilization update

———

## 2026-01-28

### eversolar-pmu-ha

- snapshot: local history cluster from 2026-01-23 to 2026-01-28 (commit-by-commit list not available in current source set)

———

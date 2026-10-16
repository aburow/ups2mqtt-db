# ups2mqtt Ideas Backlog

This is a non-binding backlog inferred from the current source and operational
documents.  Move an item into a planned requirement only after assigning an
owner, acceptance criteria, and target release.

## Next candidates

- [ ] Complete the Modbus V2 readiness evidence for each supported APC and
  CyberPower profile: live stability, baseline plans, register metadata,
  device constraints, and rollback proof.  Source: `docs/V2_READINESS.md`.
- [ ] Gather and retain known-good poll-plan dumps per driver/profile so V2
  dispatch changes can be compared against a real baseline.  Source:
  `docs/V2_READINESS.md`.
- [ ] Document model/firmware coverage and unsafe Modbus register ranges in a
  user-facing compatibility matrix.  Current documentation correctly notes
  that coverage varies, but does not provide that matrix.  Source:
  `README.md`, `docs/V2_READINESS.md`.
- [ ] Promote the existing Home Assistant OS pre-release procedure into a
  repeatable release checklist with recorded results.  The local container
  environment cannot validate Supervisor lifecycle or AppArmor.  Source:
  `README.md`, `Makefile`.
## Explicitly not planned by this document

- A custom Home Assistant integration: MQTT discovery is the intended
  integration boundary.
- New device protocols or vendors: add them only with a device/profile,
  telemetry mapping, and test evidence.
- Changes to the default 15-second minimum polling policy without measured
  scheduler and device-safety evidence.

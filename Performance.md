# UPS2MQTT Performance Report

## Scope

This report compares:

1. `100x SNMP APC` run (APC SNMP only)
2. `100x Mixed` run (mixed drivers on the simulator/profile)

It also assesses whether the current Home Assistant test installation can sustain this scale.

---

## Part 1: 100x SNMP APC (APC-only)

Reference snapshot:
- Poller metrics around `2026-05-03 23:21:48 UTC`
- Simulator stats sampled in the same validation window

Observed:
- Devices: `100`
- Polls: `172,230 started`, `172,230 succeeded`
- Failures/timeouts: `0 failed`, `0 timed out`
- Scheduler misses: `missed_capacity=0`, `missed_overlap=0`
- Backpressure: queue `0`, in-flight `0`
- Wait pressure: p95 approximately `0.105 ms`
- Event loop lag sampled low (about `0.18 ms` in that snapshot)
- Device cadence clustered around `~15s` (`~14,991 ms` average observed)
- Per-device cycle durations (sample rows): roughly `~420–520 ms`

Simulator-side validation:
- Recent cycles showed `request_count=1` per cycle for active SNMP devices
- `timeout_count=0` in sampled devices
- Very low simulator-side request service latency

Result:
- Stable operation at 100 APC SNMP devices, with no queueing, timeout, or scheduler-pressure signal.

---

## Part 2: 100x Mixed

Reference snapshot:
- Poller metrics `2026-05-04 01:17:20 UTC`
- Simulator stats sampled immediately after in this run

Observed:
- Devices: `100`
- Polls: `42,980 started`, `42,980 succeeded`
- Failures/timeouts: `0 failed`, `0 timed out`
- Scheduler misses: `missed_capacity=0`, `missed_overlap=0`
- Backpressure: queue `0`, in-flight `0`, concurrency permits available `32`
- Wait pressure: p95 approximately `0.101 ms`
- Event loop lag sampled around `~1.0 ms`
- Driver mix active across:
  - `apc_modbus_smart`
  - `apc_modbus_smt`
  - `cyberpower_modbus_single_phase`
  - `ups_snmp_apc_mib`
  - `ups_snmp_ups_mib`

Simulator-side validation:
- `160` simulated devices loaded for this scenario
- `timeouts_total=0`
- SNMP-side request counters increasing normally
- Sample active cycles show `request_count=1` and very low p95 GET latency

Result:
- Stable mixed-driver operation at 100 polled devices with no observed saturation signal.

---

## Part 3: Performance Differences

- Both runs sustain the 15s polling cadence with no scheduler misses.
- APC-only SNMP sample durations were generally higher than sampled mixed Modbus rows.
- Despite per-device timing differences, both profiles remained healthy at system level:
  - no poll failures/timeouts
  - no queue growth/backpressure
  - low wait times

Interpretation:
- The system has enough headroom in both profiles; differences are primarily in per-device execution cost, not platform instability.

---

## Part 4: HA Installation Scale Sustainability

Based on observed runs, the current Home Assistant test installation is sustaining:

- `100x APC SNMP` polling
- `100x Mixed` polling

at a 15-second cadence without signs of capacity exhaustion.

Evidence:
- zero failed/timeouts over large poll counts
- zero scheduler miss counters
- no backpressure queue buildup
- low semaphore wait and low event-loop lag

Assessment:
- Current implementation is operating in a sustainable range for this test scale.

---

## Notes / Caveats

- Simulator `missing_oid_hits` can increment in some runs; this did not correspond to poll failures/timeouts in the observed windows.
- Simulator Modbus stats currently do not expose native Modbus cadence timing the same way the poller metrics do; poller remains the authoritative source for Modbus cycle timing breakdown.

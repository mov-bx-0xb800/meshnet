# Implementation TODO

Last refreshed: 2026-07-30

This is the working checklist for turning the current demo-feasible bridge into something safer and easier to audit.

## P0 - Compliance Blockers

- Add EIRP modeling to config:
  - conducted tx power dBm,
  - external PA gain dB,
  - antenna gain dBi,
  - cable/connector loss dB,
  - measurement margin dB.
- Reject configs whose computed EIRP exceeds 27 dBm for 919-924 MHz.
- Add occupied-band calculation to config validation:
  - derive bandwidth from modem preset,
  - derive center frequency from region and slot,
  - include margin fields,
  - reject unsafe combinations.
- Keep bridge configs on `SHORT_FAST` unless `SHORT_TURBO` gets its own safe-slot validator.
- Explicitly reject `SHORT_TURBO` slot `8`.
- If `SHORT_TURBO` is enabled later, restrict to calculated safe slots, likely `2..7`, and require RF margin proof.
- Clear and verify Meshtastic `override_frequency`.
- Clear and verify Meshtastic `frequency_offset`.
- Show final computed frequency and occupied band in `doctor`.
- Add a warning/failure if live radio config reports an override or offset.
- Add a per-radio airtime ledger before relying on any `<1%` duty-cycle path.
- Do not claim certified frequency hopping unless RF behavior and conformity route are proven.
- Do not claim certified LBT unless CAD/backoff behavior and conformity route are proven.
- Add production control-lock plan for Meshtastic app, BLE admin, serial CLI, web UI, admin channel, MQTT, reset, and firmware downgrade paths.
- Confirm exact hardware, firmware, antenna, cable, enclosure, and power supply certification status.

## P1 - Reliability And Measurement

- Run real three-radio bench tests.
- Record per direction:
  - elapsed time,
  - payload hash,
  - application goodput,
  - RSSI,
  - SNR,
  - retransmitted frames,
  - failed windows,
  - radio queue drops,
  - distance and antenna height.
- Run one client alone, then two clients simultaneously.
- Test both directions for each client.
- Keep the benchmark target at or above 700 B/s worst-link application goodput unless we intentionally revise the system design.
- Confirm one current model-sized transfer, about 48,712 Flower tensor bytes for the 18-feature model, stays below 75 seconds under normal RF conditions.
- Run ten consecutive complete three-round Flower runs only after the single-transfer test is stable.
- Correct stale 15-feature model size/count references in `../TasikChiniResearch/LORA_TRANSPORT.md`, `../TasikChiniResearch/agent.md`, and older progress notes.
- Add generated calculation output to docs after every model architecture change.

## P2 - Throughput Reduction

- Evaluate float16 transfer.
- Evaluate int8 quantization.
- Evaluate sparse delta updates.
- Evaluate sending only selected layers.
- Evaluate fewer rounds.
- Evaluate fewer clients per LoRa training session.
- Evaluate local training with delayed summary upload instead of full Flower gRPC over LoRa.
- Add a benchmark target for compressed/delta mode.

## P3 - Documentation And Operations

- Refresh this folder after every RF, bridge, or model architecture change.
- Keep source URLs and retrieval dates current.
- Add a deployment runbook:
  - setup,
  - doctor output,
  - RF measurement evidence,
  - antenna inventory,
  - rollback,
  - stop-transmitting procedure.
- Add a field incident procedure:
  - disable TX,
  - reduce power,
  - change slot,
  - collect logs,
  - preserve radio state,
  - report exact time and location.

## Definition Of Done For Field Trial

- Computed EIRP is below limit with margin.
- Occupied emission stays inside the intended band with margin.
- No `override_frequency` or `frequency_offset` survives setup.
- Live `doctor` output matches expected region, preset, slot, power, channel, role, and MQTT disablement.
- Real-radio benchmark passes in both directions.
- No normal operator control can move the device into an unsafe RF configuration.
- Hardware and antenna combination are approved for the intended test scope.

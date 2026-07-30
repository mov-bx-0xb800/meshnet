# RF Compliance Working Notes

Last refreshed: 2026-07-30

Scope: ordinary LoRa / Meshtastic Short Range Communication device use in Malaysia for this repository. This is not RFID, amateur radio, PMR446, cellular, or a licensed public telecom service.

This is an engineering compliance workbench, not a final legal opinion or SIRIM submission package.

## Current Rule Set

Regulatory references to keep current:

- MCMC Class Assignment No. 2 of 2025, effective 2025-06-24.
- MCMC MTSFB TC T007:2026, Short Range Devices - Specifications, Third Revision, registered 2026-05-05.
- MCMC MTSFB TC T022:2026, Communications Equipment - Baseline Requirements, registered 2026-05-05.
- MTSFB technical-code index: https://mtsfb.org.my/technical-code/
- T007 short link: https://mtsfb.org.my/tc-srd-spec-3rd-rev
- T022 short link: https://mtsfb.org.my/tc-comm-eq-bs-req
- Public-consultation T007 third-revision PDF used for the RF table when the final MCMC PDF was blocked from this environment: https://mtsfb.org.my/wp-content/uploads/2025/04/MTSFB2409R3-Short-range-Devices-Specifications-PC.pdf

Before certification or commercial deployment, re-fetch the final MCMC PDFs and confirm the SIRIM checklist.

## Practical Malaysia 900 MHz SRC Limits

For ordinary SRC LoRa/Meshtastic:

| Band | Power limit | Extra channel-access condition |
| --- | ---: | --- |
| 916-919 MHz | 25 mW EIRP, about 14 dBm | Duty cycle below 1 percent OR certified frequency hopping OR certified LBT |
| 919-923 MHz | 500 mW EIRP, 27 dBm | No extra row condition shown |
| 923-924 MHz | 500 mW EIRP, 27 dBm | Duty cycle below 1 percent OR certified frequency hopping OR certified LBT |

Important: "No extra row condition shown" does not mean unlimited use. Interference, emissions, certification, and power limits still apply.

## Current Repo Facts

Checked implementation state:

- Flower bridge configs use `MY_919`, `SHORT_FAST`, hop limit `1`, frequency slot `8`.
- Central example conducted `tx_power` is `23`; client example conducted `tx_power` is `24`.
- Bridge validation requires `MY_919`, `SHORT_FAST`, hop limit `1`, slot `1..15`, explicit `tx_power`, `CLIENT_MUTE`, broadcast disabled, power saving disabled, and MQTT LoRa ingress/egress disabled.
- Normal non-bridge config validation only checks broad numeric ranges like slot `0..255` and tx power `0..30`. It does not globally enforce safe Malaysia slot/bandwidth combinations.
- Radio setup and bridge startup clear and verify preset mode,
  `override_frequency`, and `frequency_offset` in addition to region, preset,
  hop limit, channel slot, and TX power.
- Code-level settings are not RF proof. They do not prove final EIRP, occupied bandwidth, oscillator margin, spurious emissions, or certified FH/LBT.

Local anchors:

- `meshnet/config.flower-central.example.yaml`
- `meshnet/config.flower-client.example.yaml`
- `meshnet/config.master.yaml`
- `meshnet/config.slave.yaml`
- `meshnet/src/config.py`
- `meshnet/src/radio.py`
- `meshnet/src/flower_bridge.py`
- `meshnet/src/reliable_stream.py`

## Current Compliance Verdict

`SHORT_FAST`, slot `8`, can be directionally safe on frequency because its nominal occupied band is:

```text
center = 920.875 MHz
nominal occupied band = 920.750-921.000 MHz
```

That is inside 919-923 MHz.

But the implementation is not compliance-proven because:

- EIRP is not calculated or enforced.
- `tx_power` is conducted radio power, not EIRP.
- `override_frequency` and `frequency_offset` are cleared to zero and verified
  before the Flower bridge accepts traffic.
- No airtime ledger exists.
- No proof exists that Meshtastic `MY_919` frequency switching is certified frequency hopping.
- No proof exists that Meshtastic CAD/backoff is certified LBT.
- Production users can still alter radio settings through normal Meshtastic control paths unless firmware and access are locked.
- No SIRIM/MCMC certification evidence is attached to the exact hardware, firmware, antenna, and enclosure combination.

## EIRP Restriction

Regulated power is EIRP, not the YAML `tx_power`.

Use:

```text
EIRP dBm =
  conducted_tx_power_dBm
  + external_PA_gain_dB
  + antenna_gain_dBi
  - cable_connector_loss_dB
```

Examples:

```text
24 dBm conducted + 3.5 dBi antenna - 0.5 dB cable loss = 27.0 dBm EIRP
24 dBm conducted + 5.0 dBi antenna - 0.5 dB cable loss = 28.5 dBm EIRP, over limit
```

Working restriction:

- Treat 27 dBm EIRP as a hard ceiling.
- Use a practical production margin below 27 dBm after measurement uncertainty.
- Do not approve antenna swaps unless the EIRP math is updated.

## Occupied Band Restriction

Do not check only the center frequency.

Use:

```text
emission_low =
  center_frequency
  - occupied_bandwidth / 2
  - oscillator_error_margin
  - modulation_filter_margin

emission_high =
  center_frequency
  + occupied_bandwidth / 2
  + oscillator_error_margin
  + modulation_filter_margin
```

Working restriction:

- Keep the entire occupied signal inside 919-923 MHz unless we have a real airtime ledger or certified FH/LBT evidence.
- Treat any overlap with 923-924 MHz as invoking the 923-924 MHz condition.

## SHORT_TURBO Finding

Meshtastic lists:

| Preset | Data rate | Bandwidth | Link budget |
| --- | ---: | ---: | ---: |
| SHORT_FAST | 10.94 kbps | 250 kHz | 143 dB |
| SHORT_TURBO | 21.88 kbps | 500 kHz | 140 dB |

`SHORT_TURBO` is faster but uses 500 kHz bandwidth and has about 3 dB less link budget than `SHORT_FAST`. That means shorter range and less fade margin.

For `MY_919` with no frequency override:

```text
SHORT_FAST slot 8:
  center 920.875 MHz
  nominal occupied band 920.750-921.000 MHz

SHORT_TURBO slot 8:
  center 922.750 MHz
  nominal occupied band 922.500-923.000 MHz
```

Working restriction:

- Do not use `SHORT_TURBO` slot `8`.
- Do not use `SHORT_TURBO` slots `9` or `10`; they are in 923-924 MHz.
- If `SHORT_TURBO` is ever supported, restrict it to calculated safe slots, likely `2..7`, with explicit bandwidth and margin checks. Slot `1` touches the lower 919 MHz edge, so avoid it unless RF margin is proven.

## Airtime Restriction

The bridge sends many packets:

- DATA frames.
- ACK frames.
- POLL frames.
- repeated POLL_DONE frames.
- retries after missing ACKs.

`frame_interval_ms` is pacing, not a compliance ledger. It does not count actual airtime or enforce a legal duty cycle.

Working restriction:

- Do not rely on `<1%` compliance unless the code has a rolling per-radio airtime ledger.
- If traffic touches 923-924 MHz, the current Flower transport is likely non-compliant because one model run alone can exceed any practical `<1%` budget.

## Production Control Restriction

Production must not expose controls that permit non-compliant operation.

Controls to lock or audit:

- Meshtastic mobile app.
- BLE admin.
- Serial CLI.
- Web UI.
- Admin channel.
- Remote config packets.
- MQTT downlink.
- Environment files.
- YAML files.
- Debug and factory-test commands.
- Firmware downgrade and reset paths.

Working restriction:

- Use a Malaysia-locked firmware/build or equivalent control plan before production.
- Default and factory-reset state must be compliant.
- Radio test modes and continuous-transmit paths must not be available to normal operators.

# Meshtastic Compliance Workbench

Last refreshed: 2026-07-30

This folder is the working place for the Meshtastic / Flower-over-LoRa compliance and feasibility audit. It is intentionally practical: facts, restrictions, calculations, and the TODO list we will update as the implementation changes.

Documents:

- `RF_COMPLIANCE_WORKING_NOTES.md` - Malaysia SRD constraints, current implementation pass/fail, and forbidden configurations.
- `FLOWER_LORA_CAPACITY_MODEL.md` - model size, packet count, timing, and `SHORT_FAST` versus `SHORT_TURBO` impact.
- `IMPLEMENTATION_TODO.md` - concrete work items before any field or production use.
- `FLOWER_ROUND_BENCHMARK.md` - two-node benchmark procedure for full Flower round transfer metrics.

Current short verdict:

- Keep the bridge on `MY_919`, `SHORT_FAST`, slot `8` until code and RF tests prove another plan is safe.
- Do not use `SHORT_TURBO` on slot `8`. Its 500 kHz occupied signal reaches the 923 MHz boundary and can spill into the restricted 923-924 MHz sub-band.
- The older 47,164-byte number is one full 15-feature model transfer, not a whole federated run; current 18-feature code is about 48,712 Flower tensor bytes per full model transfer.
- Current code is demo-feasible for controlled tests, but not compliance-proven for production or unattended lake deployment.
- Legal compliance cannot be proven from YAML alone. EIRP, occupied bandwidth, spurious emissions, certification, and locked controls need hardware and firmware evidence.

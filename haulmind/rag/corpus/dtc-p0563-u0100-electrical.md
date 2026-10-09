# P0563 and U0100 — Voltage High and Lost Communication with ECM
codes: P0563, U0100
system: electrical

## P0563 — System voltage high
Sets when system voltage exceeds roughly 15.5–16 V. Almost always a failed voltage
regulator inside the alternator. Overvoltage can cook batteries and damage modules, so
treat it as **high priority** even though the truck drives normally. Advise the driver to
finish the current leg and route to an electrical shop; do not jump-start other vehicles.

## U0100 — Lost communication with ECM/PCM
A network code: another module could not hear the engine controller on the data bus for a
period. Causes: low system voltage (see P0562), a chafed CAN harness, a loose connector at
the ECM, or water intrusion. If U0100 appears only together with low-voltage codes, fix the
charging system first; the communication fault is a symptom. If it appears alone and
repeatedly, inspect the harness and connectors.

## Recommended action
- P0563: high-priority work order, electrical shop, regulator/alternator replacement.
- U0100 with P0562/SPN 168: follow the low-voltage procedure.
- U0100 alone, single occurrence: monitor; recurring: normal-priority harness inspection.

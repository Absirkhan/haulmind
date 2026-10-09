# SPN 110 — J1939 Engine Coolant Temperature
codes: SPN110-FMI0, SPN110-FMI16, SPN110-FMI3, SPN110-FMI4
system: cooling

SPN 110 is the J1939 parameter for engine coolant temperature. The FMI (failure mode
identifier) tells you what went wrong:

- **FMI 0** — data valid but above normal, most severe. The engine is overheating now.
- **FMI 16** — data valid but above normal, moderately severe. Early warning.
- **FMI 3 / FMI 4** — voltage above/below normal: sensor or wiring fault, not a real
  temperature reading.

## Interpreting with other codes
SPN 110 FMI 0 together with OBD P0217 confirms a genuine over-temperature event measured
two ways. FMI 3 or FMI 4 alone, with coolant temperature reading implausibly (for example
-40 °F or 300 °F while the truck drives normally), points to a sensor or connector problem
and should be routed to an electrical shop rather than a cooling shop.

## Recommended action
- FMI 16: advise the driver to monitor; schedule cooling inspection within 48 hours.
- FMI 0: urgent work order; driver should stop safely if the gauge keeps climbing;
  recommend out-of-service if repeated within the same shift.
- FMI 3/4: normal-priority electrical diagnosis; replace sensor or repair harness.

## Service guidance
Verify coolant level cold, pressure-test the system, check fan engagement and thermostat
operation. Download the ECM snapshot to confirm the temperature at the time of the fault.

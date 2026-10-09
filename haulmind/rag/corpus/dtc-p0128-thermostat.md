# P0128 — Coolant Thermostat Below Regulating Temperature
codes: P0128
system: cooling

P0128 means the engine took too long to reach normal operating temperature, or coolant
temperature dropped below the thermostat regulating range while running. On its own it is a
low-severity emissions code. Its value for fleet operations is as an early indicator: a
thermostat that sticks open will later stick closed, and a cooling system that cannot hold
temperature in one direction often cannot in the other.

## Symptoms
- Slow warm-up, weak cab heat in cold weather, slightly higher fuel consumption.
- Code appears, then clears after a drive cycle; may recur every few days.
- No change in coolant temperature trend when the code is intermittent and metrics are
  normal (185–205 °F steady).

## Likely causes
- Thermostat stuck open or opening early.
- Low coolant level (air pockets cause erratic sensor readings).
- Coolant temperature sensor drifting; compare to ambient after an overnight cold soak.
- Extended idling in very cold weather (normal behaviour, not a fault).

## Nuisance pattern
Many fleets see P0128 set and self-clear repeatedly on specific units without any
operational impact. If the code clears within an hour, coolant temperature stays in the
normal band and there is no accompanying P0217 or SPN 110, treat it as **informational**
and roll it into the next scheduled service rather than opening a work order. Record the
unit as a known intermittent so repeat alerts are suppressed.

## Recommended action
- Intermittent, metrics normal: no immediate action; note for next PM.
- Persistent (active more than 24 h) or paired with rising coolant temperature: schedule a
  thermostat inspection with normal priority.
- Followed by P0217 or SPN 110: escalate per the over-temperature procedure.

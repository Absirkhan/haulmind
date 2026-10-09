# P0217 — Engine Coolant Over-Temperature Condition
codes: P0217, SPN110-FMI0, SPN110-FMI16
system: cooling

P0217 sets when the engine control module sees coolant temperature above the calibrated
over-temperature threshold (typically 230–240 °F on heavy-duty diesels) for longer than the
debounce period. On J1939 networks the equivalent is SPN 110 with FMI 0 (most severe) or
FMI 16 (moderately severe). This is a protect-the-engine code: continued operation risks
head gasket failure, cracked liners and EGR cooler damage.

## Symptoms
- Coolant temperature climbing steadily over 1–4 hours of driving rather than spiking.
- Dash over-temperature lamp; possible engine derate or shutdown countdown.
- Often preceded by P0128 (thermostat below regulating temperature) when a thermostat is
  sticking, or by rising temperatures only under load or on grades.
- Coolant level warnings or sweet smell near the radiator if a leak is involved.

## Likely causes
1. Low coolant from a leak at the radiator, water pump weep hole, EGR cooler or hoses.
2. Thermostat stuck partially closed (intermittent P0128 earlier is a strong hint).
3. Fan clutch or variable-speed fan not engaging; check fan command vs. fan speed.
4. Radiator or charge-air cooler fins packed with debris; reduced airflow at low speed.
5. Failing water pump impeller; temperature rises with RPM.
6. Faulty coolant temperature sensor (compare ECM reading with an infrared gun).

## Recommended action
Treat a confirmed P0217 with rising metrics as **urgent**. Have the driver reduce load,
avoid grades, and stop at the next safe location if the temperature keeps climbing. Open a
work order with a shop that lists cooling-system work; request a pressure test, thermostat
check and fan-clutch verification. If SPN 110 FMI 0 or multiple occurrences within a shift
are present, recommend taking the vehicle out of service until inspected.

## Service guidance
Typical repair window 2–6 shop hours. Thermostat replacement 1.5 h; fan clutch 2–3 h; water
pump 3–5 h. Verify no derate history and perform a 20-minute road test watching coolant
temperature stabilise within 185–205 °F before returning the unit to service.

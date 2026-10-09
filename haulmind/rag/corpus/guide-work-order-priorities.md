# Maintenance Guide — Work Order Priorities and Out-of-Service Criteria
codes:
system: maintenance

## Priority levels
| Priority | Meaning | Examples |
|---|---|---|
| normal | bundle with next scheduled service within 7–14 days | P0401, P0420, single P0128 |
| high | shop visit within 24–48 hours; truck may finish the current leg | P0562 trend, P2002, P0087 with power loss |
| urgent | route to a shop today; do not start a new leg | P0217 with rising temperature, voltage under 12.2 V |
| critical | stop the engine; tow | P0524, SPN 110 FMI 0 repeated, oil pressure under 10 psi |

## Out-of-service recommendations
The system recommends out-of-service (a human decides) when any of these hold:
- A critical code is active, or an urgent code has recurred within the same shift.
- Coolant above 230 °F or oil pressure below 10 psi at road speed.
- Brake, steering or tire defects reported by the driver.

Grounding a truck costs roughly 800–1,200 USD per day in lost revenue plus a possible
tow, so recommendations must cite the specific codes and metric readings.

## Work order content checklist
Vehicle id and odometer, active codes with first/last seen, metric trend summary, shop
selected and why (distance, specialty, hours), priority with justification, estimated
cost, and the incident reference.

## Cost guardrail
Work orders with an estimated cost above 15,000 USD (engine or transmission replacement)
cannot be created by the automated system; they require a human-initiated purchase order.

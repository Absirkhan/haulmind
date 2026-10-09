# Service Guide — Charging System Diagnosis
codes: P0562, P0563, SPN168-FMI18, SPN168-FMI1, U0100
system: electrical

## Reading the voltage trend
Healthy heavy-duty charging systems hold 13.8–14.4 V while driving. Telematics battery
voltage is sampled with engine metrics; look at the slope, not one reading:

- Flat at 13.8–14.2 V: healthy.
- Slow decline of 0.1–0.2 V per hour: alternator losing capacity; plan a shop visit today.
- Below 12.6 V while driving with P0562 or SPN 168 active: the batteries are carrying the
  load; the truck will not restart after shutdown. High priority.
- Below 12.0 V: route to the nearest electrical-capable shop now.

## Common root causes by signature
| Signature | Likely cause |
|---|---|
| Gradual drift down over a shift | alternator brushes/diodes |
| Voltage normal at highway speed, low at idle | belt slip or weak alternator at low rpm |
| Sudden drop with U0100 codes | broken cable, blown fusible link |
| High voltage (over 15.5 V, P0563) | failed regulator |

## Recommended action
Open a **high**-priority work order with an electrical shop when the six-hour voltage delta
is below -1.0 V or P0562 has recurred twice. Message the driver to avoid shutting down at
short stops and to minimise electrical load. Estimated repair: alternator 1.5–2.5 h,
parts 400–900 USD; batteries 1 h, 600–1,200 USD for a set.

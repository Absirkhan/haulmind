# P0562 — System Voltage Low
codes: P0562, SPN168-FMI18, SPN168-FMI1
system: electrical

P0562 sets when the ECM measures system voltage below the calibrated threshold (usually
below about 12.6 V on a 12 V system while the engine runs, or 11.5 V at idle) for several
seconds. The J1939 counterpart is SPN 168 (battery potential / power input) with FMI 18
(below normal, moderately severe) or FMI 1 (below normal, most severe).

## Symptoms
- Battery voltage trending down over hours of driving: 14.0 → 13.2 → 12.4 V on telematics.
- Dim lights, slow cranking at the next start, intermittent communication codes (U0100)
  as modules brown out.
- Code recurs every 30–60 minutes once the charging system can no longer keep up.

## Likely causes
1. Alternator output failing (worn brushes, diode pack, regulator). The classic signature is
   a slow downward drift in voltage rather than a sudden drop.
2. Loose or corroded battery cables and ground straps; voltage drop under load.
3. Slipping or glazed belt; check tensioner.
4. Battery cells failing; voltage recovers while driving but collapses at shutdown.
5. Parasitic load from aftermarket equipment or reefer unit wiring.

## Recommended action
A downward voltage trend of more than 1.0 V over six hours together with recurring P0562
or SPN 168 is a **failing charging system**. Open a high-priority work order with an
electrical-capable shop before the truck strands with a dead battery. Advise the driver to
avoid unnecessary electrical load and not to shut the engine off at short stops until it is
inspected. If voltage is already below 12.2 V while driving, route to the nearest shop.

## Service guidance
Alternator output test (expect 13.8–14.4 V at 1,500 rpm), load test batteries, inspect
cables and grounds. Alternator replacement 1.5–2.5 h; battery set 1 h.

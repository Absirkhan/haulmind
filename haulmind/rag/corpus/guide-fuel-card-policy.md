# Spend Guide — Fuel Card Policy and Fraud Indicators
codes:
system: spend

## Capacity check
A single transaction cannot exceed the vehicle's usable fuel capacity. Tractor tanks in
this fleet hold 150 gallons; reefer-equipped units carry an additional 50-gallon reefer
tank that is commonly filled on the same card at the same stop. Always compare gallons to
**total** capacity (tractor plus reefer) before calling a transaction an overfill. A legit
combined fill of 185–195 gallons on a reefer unit is normal; 190 gallons on a unit without
a reefer is not.

## Location check
Compare the station coordinates with the vehicle's GPS position at the transaction time.
A gap of more than 15 miles means the card was used somewhere the truck was not. Allow
for GPS lag of one ping (about 5 minutes of travel).

## Timing check
Two fills within 90 minutes whose combined gallons exceed capacity indicate the card paid
for fuel that did not go into the truck.

## Response ladder
1. **Flag** the transaction for finance review (automatic; no driver contact needed).
2. Second anomaly on the same card within 30 days: **recommend suspending the card**
   and have a manager speak with the driver. The system never suspends cards itself.
3. Never accuse a driver of theft or fraud in a message. Driver communication about fuel
   purchases is handled by a manager in person.

## Common false positives
- Reefer fills (see capacity check).
- Odometer or GPS gaps after a device reboot; check the previous and next pings.
- Shared cards at the yard fuel island; verify card assignment before flagging.

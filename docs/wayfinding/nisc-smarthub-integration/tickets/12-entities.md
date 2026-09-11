---
status: closed
type: grilling
claimed: 2b
blocked-by: [9]
---

# Entities and device model

## Question

The live sensors the integration exposes beside its statistics, and the device they hang off.

Branches to settle:

- The four v1 sensors: cycle-to-date usage (kWh), cycle-to-date estimated cost (USD), Super Off-Peak allowance remaining (kWh), and last successful poll (timestamp). Device class, state class, units, and whether the cost sensor is `monetary` with `total` state class.
- Where their values come from: the same cycle accumulators the tariff computes, or a re-read of our own statistics.
- Tariff-specific sensors: the allowance sensor only exists for tariffs with a tiered period; how the entity set is derived from the tariff instance.
- Device: one device per service location, named from SmartHub's description or address, with the meter number as a serial; identifiers and `via_device` for the account.
- Unique ids and entity ids that survive renames and rate changes.
- Whether any diagnostic entities beyond freshness earn their place (last poll status, hours behind).

Cost of being wrong: entity and unique id changes after install are breaking; the rest is cheap to revise.

## Resolution

Grilled in one round on 2026-09-11 together with ticket 11.

**Sensors.** `cycle_usage` (kWh, `energy`, `total`, `last_reset` = cycle start); `cycle_cost` (USD, `monetary`, `total`, `last_reset` = cycle start, attributes carrying the breakdown: energy, fixed, rider, tax, adjustment); one `allowance_remaining` (kWh, `energy`, `measurement`) per tiered period the tariff declares; `last_poll` and `newest_data_hour` (timestamps, `entity_category=diagnostic`). The gap between the last two is the freshness number.

**Values** come from the coordinator's last result: the writer computes the current cycle's accumulators while pricing the window, and the routine window always contains the whole current cycle. No re-read of our own statistics.

**Device.** One per entry: identifiers `(nisc_smarthub, <account>_<location>)`, name from SmartHub's service description, manufacturer derived from the host, model the tariff's display name, `serial_number` the meter number, `configuration_url` the portal. No `via_device`.

**Unique ids** `<account>_<location>_<key>`, with the period slug appended for allowance sensors.

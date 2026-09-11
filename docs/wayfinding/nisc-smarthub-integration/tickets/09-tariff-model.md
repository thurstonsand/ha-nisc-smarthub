---
status: closed
type: grilling
claimed: 2b
blocked-by: [5]
---

# Tariff model and rate versioning

## Question

The shape of the tariff interface and its NiteFlex implementation, and how rates change over time.

Branches to settle:

- The interface: what a tariff is given (an hour's kWh per period, the cycle-to-date totals per period, the cycle's day count) and what it returns (cost for that hour, split by component). Where the service-charge smear and the allowance tiering live: in the interface, in the NiteFlex implementation, or in the writer.
- Period identity: how SmartHub's series names (`<meter> - On Peak`, `- Off Peak`, `- Super Off Pk`) map to a tariff's periods, and what happens when the poll returns a period the tariff does not know (fail the run, or price at zero and raise a repair).
- Rates as config with effective dates: the config flow stores rates the user can edit, seeded with published defaults. Decide whether a rate edit is a new dated version (so past hours keep the rates that applied then) or an overwrite (so the whole reconcile window reprices), and how a user who missed a January rate change backfills correctly. Thurston's instinct: changes come with an effective start date.
- Bill-time components from [Cobb EMC bill anatomy](05-bill-anatomy.md): which are modeled as estimates in v1 (service charge smear, maybe an estimated PCA of zero, tax?), and which are left to true-up.
- Cycle boundary: the configured day-of-month, proration when a cycle is not 30 days, and what the model does across a rate schedule change mid-cycle.
- The flat-rate seam: what a second tariff implementation would need to add, verified by sketching it, not building it.

Cost of being wrong: the tariff interface is the one abstraction every later ticket depends on; the rate-versioning choice determines whether past months can ever be repriced correctly.

## Resolution

Grilled over two rounds on 2026-09-11 against [bill-anatomy](../research/bill-anatomy.md), [niteflex-tariff](../research/niteflex-tariff.md), and [ha-integration-practice](../research/ha-integration-practice.md). The contract:

**Interface.** A tariff is stateless and prices a whole cycle at once: `price_cycle(cycle, hours, versions, actuals=None) -> list[HourCost]`. It declares `source` (`TIME_OF_USE` or `USAGE`), its period labels, and its `published_versions`. `HourUsage` is an hour start plus kWh per period; `Cycle` is start, end, and whether it is partial. `HourCost` is a breakdown (energy, fixed, rider, tax, adjustment, residual) with `total` derived; the cost statistic stores `total`, diagnostics may expose the rest. Tiering, the service-charge smear, and any proration live inside the tariff, so the writer knows nothing tariff-specific. The test seam is the pure function: table-driven cycles including the real 13-day sample ($52.99 energy + $33 service).

**NiteFlex.** Three periods matched by stripping the meter number and ` - ` separator from SmartHub series ids and comparing case-insensitively to `On Peak`, `Off Peak`, `Super Off Pk`. Super Off-Peak tiering is chronological within the cycle: the first 400 kWh at 0, the rest at the after-allowance rate. Fixed = service charge / hours in cycle. Rider = PCA factor x (hour kWh minus any Super Off-Peak kWh still inside the allowance). Tax = rate x (energy + fixed + rider). A signed `recurring_adjustment` per cycle is smeared like the service charge and sits outside the tax base. Money is float throughout.

**Rate versions.** One dated bundle per version: `effective_from`, `on_peak_rate`, `off_peak_rate`, `super_off_peak_rate`, `super_off_peak_allowance`, `service_charge`, `pca_factor` (default 0), `sales_tax_rate` (default 0.0775), `recurring_adjustment` (default 0). Stored as an append-only list in entry **data** (Core's letter: required settings live in data), edited through a reconfigure-flow menu: add a version, remove the newest, change the cycle day. A version governs every hour from local midnight on `effective_from`, mid-cycle included. Future-dated versions are inert until their day; a past-dated version triggers a reconcile from that day (ticket 10 owns the mechanics). The tariff ships its published versions; setup seeds the first from them; when a release ships a published version newer than the newest configured one, the integration raises a repair issue and never auto-applies.

**Cycles.** `[00:00 local on the cycle day of month M, 00:00 local on the cycle day of M+1)`, day clamped to the month's last day, first cycle from the connect date. The service charge and allowance are flat per cycle regardless of length, including the partial first cycle; the true-up carries the actual.

**Failure.** An unknown period label, or an hour present in `USAGE` but in no period series, writes usage, writes no cost, and raises a repair issue naming the label. The fix is a release (the label table is a constant of the tariff); backfill is the next run, since the reconcile window still covers the hours.

**Finalized cycles.** `actuals` from the cycle record (service charge, PCA factor, tax, read dates, bill total) override the estimates; the difference to the bill total is smeared across the cycle's hours as `residual`, stored on the cycle record and visible in diagnostics, so a finalized cycle's cost sum equals the bill to the cent and a large residual signals a missing line.

**Flat-rate seam.** A flat tariff declares `source = USAGE` and one period; the writer emits per-period usage statistics only when a tariff has more than one period. Nothing else changes.

**Out of scope.** One-off per-cycle adjustments (fees, capital credit) beyond what the true-up captures. Whether a shared rate-data source exists is [ticket 15](15-shared-rate-sources.md).

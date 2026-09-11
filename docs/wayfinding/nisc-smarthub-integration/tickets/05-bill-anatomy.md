---
status: closed
type: research
claimed: subagent
blocked-by: []
---

# Cobb EMC bill anatomy

## Question

Every line that appears on a Cobb EMC residential NiteFlex bill for a service location inside the City of Roswell, Georgia, and how each is computed, so the tariff model knows which charges are per-kWh, per-period, per-cycle, or bill-time-only and what a cycle true-up has to reconcile.

Pin against primary sources (Cobb EMC's rate schedules and "understand your bill" pages, the PCA schedule PDF, Georgia Department of Revenue, City of Roswell code):

- Energy charges by period and tier, service charge, minimum charge, and proration rules (the rate schedule is transcribed in [`research/niteflex-tariff.md`](../research/niteflex-tariff.md); confirm and extend, don't redo).
- The Power Cost Adjustment: how it is set, how often it changes, its sign history, whether the current value is published anywhere, and the exact exemption for the first 400 Super Off-Peak kWh.
- Georgia state and local sales tax on residential electricity: rate for Roswell / Fulton County, what base it applies to (energy only, or service charge too, or after PCA), and whether any residential exemption applies.
- Any franchise fee or municipal charge for an EMC customer inside Roswell city limits.
- Operation Round Up mechanics (the account is enrolled): rounds the total to the next dollar, before or after tax.
- Anything else Cobb EMC itemizes: deposits, late fees, security lighting, capital credit retirements.

Output: a line-item table with formula, source, and whether it is knowable from the published schedule or only from the posted bill. That partition is what the true-up design consumes.

## Resolution

[`research/bill-anatomy.md`](../research/bill-anatomy.md) tables every line a NiteFlex bill can carry and splits them into what the published schedules fix (energy by period, tier and allowance, service charge, minimum charge, the 7.75% Fulton sales tax rate) and what only a posted bill reveals (the PCA factor, short-cycle proration, the taxable base's exact rounding, the round up, one-off fees, the annual capital credit). Sales tax falls on total charges billed including the service charge, Operation Round Up rounds the post-tax total, and the PCA rider publishes a formula but no factor, so the first bill is still the only way to learn its value.

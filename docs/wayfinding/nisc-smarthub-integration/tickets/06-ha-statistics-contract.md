---
status: closed
type: research
claimed: subagent
blocked-by: []
---

# Home Assistant statistics import contract

## Question

The exact contract for an integration that writes and rewrites external long-term statistics in Home Assistant 2026.9, so the statistics writer can be designed against facts rather than folklore.

Pin against `home-assistant/core` at the 2026.9.x tag (the appliance runs 2026.9.1), the developer docs, and the developer blog:

- `async_add_external_statistics` and `async_import_statistics`: signatures, `StatisticMetaData` fields as of 2026.9 (`unit_class`, `mean_type`, `has_sum`, `source`, `name`), which are mandatory, and what the recorder validates (tz-aware top-of-hour `start`, `source` matching the id prefix, unit rules for `kWh` and for a currency).
- Upsert semantics: what happens on re-import of an existing `(statistic_id, start)`; whether `sum` and `state` are both replaced; whether a rewrite of one hour without rewriting later hours corrupts the derived `change`.
- How the recorder computes `change` and `sum` for display, and what the Energy dashboard reads for `stat_energy_from` and `stat_cost` (5-minute vs hourly tables, and when hourly is materialized).
- How to read back the last stored `sum` before a given time from inside an integration (the reconcile window needs a seed), and how to read a range (`statistics_during_period` in the recorder executor).
- Deleting or clearing an external statistic (`recorder/clear_statistics`), for migration off gagata's ids and for a "full reconcile" repair path.
- Core's `opower` integration as the first-party template: how its coordinator writes usage and cost statistics, what metadata it uses for the cost statistic (unit, unit_class), and how it seeds from the last stored sum.
- `pytest-homeassistant-custom-component`: how tests exercise statistics writes and read them back.

Output: a reference the design can cite, with a short "rules we must obey" list at the top.

## Resolution

The [statistics contract reference](../research/ha-statistics-contract.md) pins the import, metadata, replacement, seed/read, clear, Energy, Opower, and testing contracts to Core 2026.9.1. It corrects the earlier research's metadata deprecation deadline and external cost-validation claims, distinguishes Opower's unitless append-only costs from our USD reconcile writer, and records the cumulative-tail continuity rules.

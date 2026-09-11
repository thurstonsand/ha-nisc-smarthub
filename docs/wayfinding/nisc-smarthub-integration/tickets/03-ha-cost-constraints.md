---
status: closed
type: research
claimed: 2b
blocked-by: []
---

# Home Assistant cost constraints

## Question

Why does Home Assistant refuse a price on an externally sourced usage statistic, what path remains for showing cost on the Energy dashboard, and what rules govern writing and rewriting external statistics?

## Resolution

[`research/ha-cost-constraints.md`](../research/ha-cost-constraints.md) traces the refusal to `_reject_price_for_external_stat` in `energy/data.py` and the entity-subscription design of the cost sensor in `energy/sensor.py`; `stat_cost` pointing at our own USD external statistic is the supported hook, and `cost_adjustment_day` is vestigial in both core and frontend. Imports upsert by `(statistic_id, start)` with tz-aware top-of-hour starts, and because HA derives `change` by differencing cumulative `sum`, any rewrite must extend forward to the newest point. Core's `opower` integration is the first-party precedent for a cost statistic.

---
status: closed
type: grilling
claimed: 2b
blocked-by: [9, 10, 11, 12, 13]
---

# Design doc and implementation plan

## Question

Assemble the resolved tickets into `docs/designs/01-nisc-smarthub.md` through `/grill-me` Gates 2 and 3: the design with its decisions and tradeoffs, then a tracer-bullet implementation plan whose first phase lands one usage statistic on the local dev instance end to end and whose last phase is the appliance cutover runbook.

This ticket is the map's destination. It closes when the design doc is `Accepted` and the plan is handoff-safe.

## Resolution

[`docs/designs/01-nisc-smarthub.md`](../../../designs/01-nisc-smarthub.md), accepted 2026-09-11. Sixteen decisions, an eight-phase tracer-bullet plan whose second phase puts one usage statistic on a fresh local instance end to end and whose last phase is the appliance cutover as a runbook. Two alternatives stay open: declaring HA's runtime dependency closure for `--skip-pip`, and a bill reader once the first bill posts.

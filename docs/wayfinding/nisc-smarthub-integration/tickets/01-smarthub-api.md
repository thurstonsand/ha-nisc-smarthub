---
status: closed
type: research
claimed: 2b
blocked-by: []
---

# SmartHub API surface

## Question

What does the NISC SmartHub API expose for a Cobb EMC residential account: how does authentication work, which endpoints return account, billing, and usage data, what shapes do they return, and does anything return cost or time-of-use classification?

## Resolution

[`research/smarthub-api.md`](../research/smarthub-api.md) records the endpoints exercised live on 2026-09-10/11. Authentication requires a TOTP code alongside the password; `user-data` and `billing` are GETs that yield account, service-location, rate-schedule, and cycle facts; `utility-usage/poll` is an async POST returning a `USAGE` entry and a `TIME_OF_USE` entry whose per-period series exactly partition the hours. No endpoint returns cost.

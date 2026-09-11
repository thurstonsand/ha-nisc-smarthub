---
status: closed
type: grilling
claimed: 2b
blocked-by: [9]
---

# Config flow, options, and credentials

## Question

Everything the user enters and can later change, and how the integration validates it.

Branches to settle:

- Initial flow steps: host, email, password, TOTP secret; then account and service location (discovered from `user-data` and offered as a select, or typed); then tariff selection and its rate fields with defaults; then billing-cycle start day and poll interval. What is `data` (identity) versus `options` (editable).
- Validation at each step: a live auth check, a live `user-data` read, and whether to require one successful poll before creating the entry.
- Unique id: account number + service location, so a second location is a second entry.
- Reauth flow when TOTP or password fails; reconfigure flow for host or account changes; options flow for rates, cycle day, and interval; and the effective-date behavior decided by [Tariff model and rate versioning](09-tariff-model.md).
- Entry `VERSION` / `MINOR_VERSION` and migration expectations before 1.0.
- Diagnostics: what is dumped and what `async_redact_data` must hide (credentials, TOTP secret, account, meter, address).
- The MCP-driven install path on the appliance: `ha_set_integration(domain=..., config=...)` drives this flow, so multi-step and select fields must work without a browser.

Cost of being wrong: config entry shape changes after install need migrations; a wrong unique id merges or duplicates locations.

## Resolution

Grilled in one round on 2026-09-11 together with ticket 12.

**Steps.** (1) Portal: host, email, password, TOTP secret; live auth. (2) Location: account and service location selected from `user-data`, auto-skipped when there is exactly one. (3) Tariff: the tariff whose declared rate schedule codes match the location's `activeRateSchedules` (NiteFlex declares `NFON`, `NFOFF`, `NFSOF`) is preselected; the user may override; no match aborts with a message naming the codes seen. Rate fields are seeded from the tariff's newest published version with its `effective_from`; billing cycle day on the same step. Before creating the entry, one hourly poll of the last three days must succeed, so a location without interval data fails setup with a message rather than at the first refresh. The rates themselves are never fetched: no API carries them ([ticket 15](15-shared-rate-sources.md)).

**Data vs options.** Data: credentials, account, location, tariff, rate versions, cycle day. Options: poll interval only.

**Unique id** `<account>_<location>`. A second service location is a second entry; the same location twice aborts.

**Reconfigure** is a menu: add rate version, remove newest rate version, billing cycle day, credentials. **Reauth** (on `ConfigEntryAuthFailed`) asks for password and TOTP secret. **Options** holds the poll interval.

**Diagnostics** redact by key (password, TOTP secret, token) and by value: every occurrence of the account number, location id, meter number, address, customer name, and security hint anywhere in the dump, statistic ids included. The `billing` response carries the hint answer in plaintext.

**Schema.** `VERSION=1, MINOR_VERSION=1`; the shape may break freely until the first tagged release, after which migrations are mandatory. Expect a long pre-release.

# NISC SmartHub integration: usage and cost statistics for the Energy dashboard

## Status

Accepted

## Decision Summary

Build `nisc_smarthub`, a HACS custom integration that pulls hourly usage from a NISC SmartHub member portal, keeps the utility's own time-of-use classification of every hour, prices it under a versioned tariff, and writes usage and cost as external long-term statistics that the Energy dashboard reads directly. It replaces `gagata/ha-smarthub-energy-sensor` rather than extending it, because cost has to be computed from the same poll that produced the usage, and the price of a past hour has to be rewritable when rates change or a bill posts. The tradeoff is owning a scraper and a reconcile loop instead of a thin sensor.

## Problem Statement / Background

Home Assistant's Energy dashboard shows kWh for the Cobb EMC grid source today, fed by gagata's integration, and no dollars. The dashboard's own price fields are rejected for that source: `energy/data.py` refuses `entity_energy_price` and `number_energy_price` on any statistic that is not an entity, because the cost sensor in `energy/sensor.py` computes cost by subscribing to an entity's state changes, and an external statistic has none. The only supported hook is `stat_cost`, a second statistic in currency that an integration writes itself. Core's `opower` integration does that for utilities whose portals report cost. SmartHub reports none: the `utility-usage/poll` endpoint returns `USAGE` and `TIME_OF_USE` series and nothing priced.

Pricing NiteFlex is not a multiplication. Three periods carry three rates, the first 400 kWh of Super Off-Peak in each billing cycle are free, a $33 service charge and a Power Cost Adjustment rider arrive per cycle, sales tax applies to the sum, and the PCA factor is not published anywhere; it appears on the bill. So the cost of any hour depends on cycle-to-date state, on rates that change each January, and on values that are only knowable weeks later. A cost that is right for trends has to be recomputable: when a rate version is added with a past date, when the cycle day changes, and when a bill finalizes a cycle.

Scenarios that shaped the design:

- The user notices in March that NiteFlex rates changed on January 1. They add a rate version dated January 1 and every unfinalized hour since then reprices; December does not.
- The first bill posts in October for a short cycle starting 2026-08-28. The user runs `finalize_cycle` with the bill's service charge, PCA factor, tax, read dates, and total. That cycle's cost sum now equals the bill to the cent, and the next cycle's start moves to the bill's read date.
- SmartHub revises last Tuesday's 3pm from 1.2 kWh to 1.9 kWh. The next routine run logs the revision, rewrites that hour's usage and every later cumulative sum, and the dashboard bar changes without a spike.
- Every sensor is disabled. Statistics keep being written.

Previous attempts: gagata's integration proved the portal's authentication and poll endpoints and is the source of the client logic. It discards the `TIME_OF_USE` series, re-imports only two days back, and never backfills a missed hour. A PR upstream was considered and declined; the cost work needs the writer to own the whole history.

## Goals

- The Energy dashboard shows kWh and dollars for the grid source, hourly, from the account's connect date forward.
- A past hour's price is always recomputable from the rates that applied to it, and a finalized cycle matches its bill.
- The integration survives restarts, runs unattended, and writes statistics with no entities enabled.
- Development is one command from a clean clone, on a laptop or in an Amp orb, with tests that exercise the real recorder.
- A second rate schedule, or a second utility on SmartHub, is a new tariff class plus data, with no change to the writer.

## Non-Goals

- Reading bills automatically. The true-up hook exists; a bill reader waits for the first bill and the API research it enables.
- A flat-rate tariff implementation, net metering, or return-to-grid statistics.
- One-off per-cycle adjustments entered by hand (fees, capital credit). The residual absorbs them at true-up.
- Submission to the HACS default store.
- The appliance cutover from gagata's integration. A runbook is the last phase of this plan; executing it is the next effort.

## Exposed Shape

### Config entry

One entry per service location, unique id `<account>_<location>`. Entry data holds host, email, password, TOTP secret, account number, service location id, tariff key, the rate version list, and the billing cycle day. Options hold the poll interval (default 360 minutes). `VERSION = 1`, `MINOR_VERSION = 1`; the shape may break without migration until the first tagged release.

Flows:

- **User**: (1) portal host, email, password, TOTP secret, verified by a live login; (2) account and service location from `user-data`, skipped when there is exactly one; (3) tariff, preselected from the location's `activeRateSchedules` codes (NiteFlex declares `NFON`, `NFOFF`, `NFSOF`), overridable, aborting with the codes seen when none matches; rate fields seeded from the tariff's newest published version with its `effective_from`; billing cycle day. A three-day hourly poll must succeed before the entry is created.
- **Reauth**: password and TOTP secret.
- **Reconfigure**: a menu with add rate version, remove newest rate version, billing cycle day, credentials.
- **Options**: poll interval.

### Rate version

```python
@dataclass(frozen=True)
class RateVersion:
    effective_from: date          # governs hours from local midnight on this day
    on_peak_rate: float           # $/kWh
    off_peak_rate: float
    super_off_peak_rate: float    # after the allowance
    super_off_peak_allowance: float  # kWh per cycle, free
    service_charge: float         # $ per cycle, smeared per hour
    pca_factor: float             # $/kWh, signed; allowance kWh exempt; default 0
    sales_tax_rate: float         # on energy + fixed + rider; default 0.0775
    recurring_adjustment: float   # $ per cycle, signed, untaxed; default 0
```

The fields are NiteFlex's. The list is append-only; the tariff prices each hour under the newest version whose `effective_from` is at or before the hour's local day. Future-dated versions are inert until their day.

### Statistics

Five external statistics, all `has_sum=True`, `mean_type=NONE`, `source="nisc_smarthub"`:

- `nisc_smarthub:<account>_<location>_usage`, kWh, `unit_class="energy"`
- `nisc_smarthub:<account>_<location>_usage_<period_slug>`, one per tariff period when the tariff has more than one (`on_peak`, `off_peak`, `super_off_peak`)
- `nisc_smarthub:<account>_<location>_cost`, `"USD"`, `unit_class=None`

Each row is one hour: `state` is the hour's value, `sum` is cumulative from the history floor. The Energy dashboard grid source sets `stat_energy_from` to the usage id and `stat_cost` to the cost id.

### Entities

One device per entry: identifiers `(nisc_smarthub, <account>_<location>)`, name from SmartHub's service description, manufacturer derived from the host, model the tariff's display name, serial number the meter, configuration URL the portal.

Sensors, unique id `<account>_<location>_<key>`:

- `cycle_usage`: kWh, `energy`, `total`, `last_reset` at cycle start
- `cycle_cost`: USD, `monetary`, `total`, `last_reset` at cycle start; attributes carry the breakdown (energy, fixed, rider, tax, adjustment)
- `allowance_remaining_<period_slug>`: kWh, `energy`, `measurement`; one per tiered period the tariff declares
- `last_poll`, `newest_data_hour`: timestamps, diagnostic category

Values come from the coordinator's last result. Entity properties never touch the network or the recorder.

### Service actions

```yaml
nisc_smarthub.reconcile:
  config_entry_id: str
  from: date = history floor        # full pass from this day; never clears rows

nisc_smarthub.finalize_cycle:
  config_entry_id: str
  cycle_start: date                 # identifies the cycle
  read_start: date                  # actual read dates replace the calendar bounds
  read_end: date
  service_charge: float
  pca_factor: float
  tax: float                        # the bill's tax line
  bill_total: float

nisc_smarthub.unfinalize_cycle:
  config_entry_id: str
  cycle_start: date
```

`finalize_cycle` writes a cycle record and triggers a full reconcile from that cycle's start. A future bill reader calls the same function.

### Repair issues

- Unknown period label, or an hour present in `USAGE` but in no period series: usage written, cost skipped, issue names the label. Fixed by a release; cleared by the next successful run.
- A release ships a published rate version newer than the newest configured one: issue asks the user to add a version. Never auto-applied.

### Diagnostics

The entry dump redacts by key (password, TOTP secret, token) and by value: every occurrence of the account number, location id, meter number, address, customer name, and security hint, statistic ids included. The `billing` response carries the hint answer in plaintext.

### Boundaries

**Client to portal.** `smarthub/` is a package with no Home Assistant imports and an injected `aiohttp.ClientSession`. Operations: `login(email, password, totp_secret) -> Session`, `user_data() -> list[Customer]`, `billing(account) -> BillingSummary`, `service_location(id) -> ServiceLocation`, `poll_hourly(account, location, start, end) -> PollResult`. `PollResult` carries a `usage: dict[datetime, float]` and `periods: dict[str, dict[datetime, float]]` keyed by the label after the meter prefix. Failures: `AuthError` for 401 and the 500 "could not be verified" body, `PollTimeout` when the poll stays `PENDING` after 5 retries at 4 s, `ClientError` for the rest. The client parses and conforms; nothing downstream sees raw JSON.

**Coordinator to tariff.** `Tariff.price_cycle(cycle: Cycle, hours: Sequence[HourUsage], versions: Sequence[RateVersion], actuals: CycleActuals | None) -> list[HourCost]`. `Cycle` is start, end, partial flag. `HourUsage` is start plus kWh per period. `HourCost` is energy, fixed, rider, tax, adjustment, residual, with `total` derived. A tariff also declares `key`, `display_name`, `source` (`TIME_OF_USE` or `USAGE`), `periods` (labels and slugs), `tiered_periods`, `rate_schedule_codes`, and `published_versions`. The tariff owns tiering, smearing, and proration; the writer owns nothing tariff-specific.

**Writer to recorder.** Reads with `statistics_during_period` in the recorder executor, always with the full production signature and `period="hour"`. Writes with `async_add_external_statistics` on the event loop, with concrete lists (never generators) of tz-aware UTC top-of-hour points. Never clears.

**Cycle records to storage.** `helpers.storage.Store` at `.storage/nisc_smarthub.<entry_id>`, version 1: `{"cycles": {"<cycle_start>": {"read_start", "read_end", "service_charge", "pca_factor", "tax", "bill_total", "residual"}}}`.

## Call Stacks and Data Flow

### Setup

```txt
async_setup_entry(hass, entry)
  SmartHubClient(async_get_clientsession(hass), ...)
  CycleStore(hass, entry).async_load()
  NiscSmartHubCoordinator(hass, entry, client, tariff, store)
    .async_config_entry_first_refresh()          -> ConfigEntryNotReady | ConfigEntryAuthFailed
    .async_add_listener(lambda: None)            entry-owned; removed on unload
  entry.runtime_data = coordinator
  async_forward_entry_setups(entry, [SENSOR])
  register services once per domain (async_setup)
```

### Poll and reconcile

```txt
NiscSmartHubCoordinator._async_update_data
  plan = self.plan_window()                       routine | full (flag set by setup, config change, finalize, action)
  poll = client.poll_hourly(account, location, plan.start, now)      PENDING -> retry -> COMPLETE
  hours = conform(poll, tariff.periods)          unknown label -> mark cost_skipped, raise repair, keep usage
  seeds = writer.read_seeds(plan.start)           statistics_during_period([floor, W)) per id, last sum
    empty and W > floor -> plan = full from floor, seeds = 0, re-poll if needed
  existing = writer.read_window(plan.start, now)
  cycles = split_into_cycles(hours, cycle_day, store.finalized_bounds())
  costs = [tariff.price_cycle(c, c.hours, versions, store.actuals(c)) for c in cycles]
  rows = writer.build_rows(hours, costs, seeds)   cumulative sums per id
  diff = writer.diff(existing, rows)              tolerance 1e-9 kWh / 1e-6 USD; log revisions
  async_add_external_statistics(...) per id for diff rows
  return CoordinatorData(cycle_to_date, allowance_remaining, last_poll, newest_data_hour, cost_skipped)
```

Data shape across the boundaries:

```txt
poll JSON (ELECTRIC[USAGE], ELECTRIC[TIME_OF_USE])
  -> PollResult (epoch ms -> aware UTC datetimes, kWh floats, labels stripped of meter prefix)
  -> list[HourUsage] (one per hour, kWh per period; hours absent from the poll are absent)
  -> per-cycle list[HourCost]
  -> StatisticData rows (start UTC, state, sum) per statistic id
  -> recorder Statistics table
```

### Finalize a cycle

```txt
service finalize_cycle
  store.set_actuals(cycle_start, CycleActuals(...))
  store.async_save()
  coordinator.request_full(from=cycle_start)
  coordinator.async_request_refresh()
    -> reconcile as above; the finalized cycle prices with actuals, residual = bill_total - sum(components), smeared per hour
    -> the next cycle's start becomes read_end
```

### Reconfigure

```txt
reconfigure flow: add rate version
  validate effective_from unique; append; async_update_reload_and_abort
  -> async_unload_entry / async_setup_entry
  -> coordinator.request_full(from=min(effective_from, oldest unfinalized cycle start))
```

## Design Decisions

### 1. Replace gagata's integration; one scraper for usage and cost

Two scrapers with separate revision windows disagree about the hours they sit next to. Cost has to be computed from the same poll that produced the usage, and the writer has to own every hour it prices. The domain is `nisc_smarthub` so both can coexist on the appliance during cutover; the statistics live in separate namespaces and the Energy grid source is repointed.

### 2. Stateless tariff that prices a whole cycle

`price_cycle(cycle, hours, versions, actuals)` is a pure function. The reconcile window always starts at a cycle boundary, so the tariff never sees a partial cycle it has to reason about, and tiering, smearing, and proration are trivial inside it. Tests are table-driven against hand-computed cycles, including the real first 13 days ($52.99 energy, $33 service). The alternative, hour-at-a-time with caller-supplied accumulators, pushes every tariff-specific accumulator into the writer's vocabulary.

### 3. Rate versions, dated, append-only, in entry data

A rate change comes with an effective date, and the user may notice it late or want to enter it early. Dated versions answer both; a single overwritable rate set answers neither, and a two-version hybrid handles exactly one missed change. Versions govern hours from local midnight on `effective_from`, mid-cycle included, because that is how the schedule states it and the utility's straddling-cycle proration is unknown. They live in entry data with a reconfigure flow, following Core's rule that required settings are data; the options flow holds only the poll interval. Cost of reversing: a migration moving a list between two keys.

### 4. Published defaults seed and warn; never auto-apply

The tariff ships its published versions. Setup seeds the first from them. When a release ships a newer published version than the newest configured, a repair issue asks the user to add a version. A user on a negotiated or grandfathered rate must never have history rewritten by a package update. Research confirmed no machine-readable source exists to pull rates from: the URDB's Cobb EMC entries froze in 2018 without NiteFlex, and SmartHub's `secured/rates` is a CORS preflight handler.

### 5. Every write ends at the newest hour

The recorder stores cumulative `sum` per row and derives `change` by subtracting consecutive rows. Rewriting one hour without rewriting every later row leaves a compensating error at the first unrewritten row. Making "the window ends at now" an invariant turns the tail replay into the ordinary case.

### 6. Two window tiers

Without true-ups, "oldest unfinalized cycle to now" is all of history, and the user may never do a true-up. Routine runs cover the cycle containing `now - 7 days` to now, at most two cycles, comfortably past SmartHub's revision horizon. A full pass from the oldest unfinalized cycle or the floor runs on the first run, any rate version or cycle-day change, any finalize or unfinalize, and the `reconcile` action. Every event that can change a past hour's price is on that list.

### 7. Seeds from a range read; a gap promotes the run

There is no "last sum strictly before W" API. The seed is the last row of `statistics_during_period([floor, W))` per id, in the recorder executor. If that read is empty and W is past the floor, there is a hole before the window, and the run promotes itself to a full pass from the floor rather than seeding 0 or failing. Reconciliation over execution.

### 8. Diff before write, log revisions

Existing rows in the window are read back and only rows whose state or sum differ are written. The write savings are incidental; the point is that a SmartHub revision of a past hour is logged with old and new values, and a test can assert that a revision fixture changes exactly one usage row and the cost tail.

### 9. Missing hours get no row

A zero row is a lie the dashboard shows as a real zero. Sum continuity tolerates absent rows. Smear and tiering use the cycle's nominal hour count from the calendar, so a missing hour's share of fixed charges is absent until the hour arrives.

### 10. Cycle records in a per-entry Store; a service action is the true-up hook

Config in the entry, observed facts in `helpers.storage.Store`. `finalize_cycle` writes actuals and triggers a full pass; the finalized cycle takes the bill's service charge, PCA factor, tax, and read dates as overrides, and the difference to the bill total is smeared as a stored residual, so the cycle's sum equals the bill to the cent and a large residual signals a missing line. Read dates replacing calendar bounds can shift the next unfinalized cycle's start, which is why finalize triggers a full pass. A bill reader, when it exists, calls the same function.

### 11. Coordinator with an entry-owned listener

A `DataUpdateCoordinator` polls only while it has listeners, and the statistics are the product, not the sensors. The entry registers a no-op listener in `async_setup_entry` and removes it on unload. This keeps HA's backoff, reauth, and `UpdateFailed` semantics.

### 12. Fail loud on unknown labels, but keep usage

An unknown `TIME_OF_USE` label is a tariff definition change. Usage is still written (it needs no mapping), cost is skipped for the run, a repair issue names the label, and the fix is a release. Backfill needs no mechanism: the window rewrite covers the hours on the next successful run, and a cycle cannot finalize without cost.

### 13. Float money

Per-hour costs are fractions of a cent, the recorder stores float64, and cent-exact reconciliation happens at true-up on cycle totals where a float sum of ~700 terms is exact far below a cent.

### 14. Idiomatic HACS development loop, orb-ready

`config/configuration.yaml` is tracked in the blueprint's shape (`default_config`, debug, logger); the rest of `config/` is ignored. `mise run dev` symlinks `custom_components` and runs `hass --debug`; `mise run dev:bootstrap` performs headless onboarding and mints an agent token, an internal API whose breakage on upgrade is expected maintenance. `homeassistant==2026.9.1` and `pytest-homeassistant-custom-component==0.13.364` are pinned together. `hass` installs its runtime closure at first start for now; declaring the closure in uv and switching to `--skip-pip` is the first follow-up, and orbs make it worth doing early because a snapshot without the closure pays a pip install per fresh orb. `.agents/setup`, `.amp/services.yaml`, and `.agents/resume` make the loop work in an orb from a clean clone.

### 15. Recorded fixtures, scrubbed deterministically

`scripts/capture_fixtures.py` pulls real responses through 1Password-held credentials and applies a fixed replacement map to every string, failing if any real value survives. Recorded fixtures encode what the API returns, including fields nobody reads yet; deterministic replacement keeps identifiers consistent across the config flow, parser, and statistics id tests.

### 16. Client bundled, seam clean

`custom_components/nisc_smarthub/smarthub/` has no HA imports and an injected session. Core's rule about separate PyPI packages exists so Core does not carry vendor code; with one consumer, the seam is what matters, and it makes extraction mechanical if a second consumer appears.

## Edge Cases & Failure Modes

- **MFA missing or wrong:** the portal returns HTTP 500 with "Your data could not be verified"; the client raises `AuthError`, the coordinator raises `ConfigEntryAuthFailed`, reauth starts. Not retried as a transient error.
- **Poll never completes:** `PollTimeout` after 5 retries at 4 s becomes `UpdateFailed`; HA's backoff applies.
- **Partial poll (USAGE without TIME_OF_USE):** usage written, cost skipped, repair issue.
- **Hour in USAGE but in no period series:** treated as an unknown label.
- **Newest hours absent:** SmartHub lags about a day; nothing is written for them; `newest_data_hour` shows what arrived.
- **DST transitions:** hours are aware UTC at the boundary; the tariff's "local midnight" for version selection and the cycle's local bounds are computed in the configured timezone. A test covers a window spanning both transitions.
- **Cycle day 31 in a 30-day month:** clamps to the last day.
- **First cycle:** starts at the connect date, flat $33 and 400 kWh regardless of length; the true-up carries the actual.
- **Rate version with a future date:** inert until its day; the routine window picks it up.
- **Rate version added inside a finalized cycle:** the finalized cycle does not reprice; the bill is its truth.
- **Cycle-day change:** a full pass; unfinalized cycles re-split; finalized cycles keep their read-date bounds.
- **Every sensor disabled:** the entry-owned listener keeps the coordinator polling.
- **Two entries for the same location:** the flow aborts on unique id.
- **Reinstall:** statistic ids are derived from account and location, so a reinstall repairs rather than orphans.
- **`uv sync` after `hass` installed its closure:** the closure is pruned; the next `hass` start reinstalls it. Documented in DEV.md until the closure is declared.

## Alternatives

### PR upstream to gagata's integration

- **Status:** Rejected
- **Decision:** the cost work needs the writer to own the whole history and the reconcile window; upstream's two-day re-import and "skip rows at or before the seed" policy are incompatible, and the user prefers not to depend on an upstream review cycle.
- **Discussion:** upstream's client logic is ported with attribution.

### Coexist with gagata's integration and only add cost

- **Status:** Rejected
- **Decision:** two scrapers with separate revision windows disagree about the hour they sit next to, and cost has to be computed from the same poll as usage.

### `cost_adjustment_day` as the service-charge hook

- **Status:** Rejected
- **Decision:** the field exists only in Core's schema and legacy migration and in the frontend as a passthrough; nothing reads it into a number.

### A single overwritable rate set

- **Status:** Rejected
- **Decision:** cannot express a change noticed late without repricing earlier cycles under the wrong rates.

### Seed from a shared rate database

- **Status:** Rejected
- **Decision:** ticket 15 found none. The design keeps the seam (a tariff class plus data) for a hand-transcribed second utility.

### Proportional scaling to the bill total at true-up

- **Status:** Rejected
- **Decision:** hides which component was wrong. Overrides plus a stored residual keeps the estimate honest.

### `--skip-pip` with a declared closure from day one

- **Status:** Open
- **Open Issue:** the closure depends on the dev config, which is still moving; orbs make a declared closure valuable.
- **Next step:** after Phase 2, inventory what `hass` installed on first start and declare it in the dev group.

### Bill reader against the SmartHub billing API

- **Status:** Open
- **Open Issue:** with zero bills issued, no per-bill itemization endpoint could be verified.
- **Next step:** when the first bill posts (~October 2026), probe `billing` and siblings and write a research ticket; the `finalize_cycle` path is its target.

## Implementation Plan

Tracer bullets: each phase lands a narrow, complete path through every layer it touches and is demonstrable on the local instance. Implementation runs on Opus per the project's model policy.

- [ ] Phase 1: Client and fixtures
  - Goal: a typed SmartHub client with recorded, scrubbed fixtures and pure tests, plus the integration skeleton HACS would install.
  - Files: `custom_components/nisc_smarthub/{manifest.json,__init__.py,const.py}`, `custom_components/nisc_smarthub/smarthub/{__init__.py,client.py,models.py}`, `hacs.json`, `scripts/capture_fixtures.py`, `tests/fixtures/*.json`, `tests/smarthub/test_client.py`, `pyproject.toml` (Core's Ruff set, HA and test plugin pins, pytest-cov gate), `mise.toml` (`test` with coverage).
  - Work: port gagata's auth and poll logic with attribution into an HA-free package with an injected session; conform `user-data`, `billing`, `service-locations/<loc>`, and hourly/daily polls into dataclasses; strip the meter prefix from period labels; classify the 500 "could not be verified" body as `AuthError`; write the capture script with the deterministic scrub map and leak check; record fixtures for a three-day hourly poll, a daily poll, `user-data`, `billing`.
  - Validation: `mise run check`; `mise run test` with the pure client tests green; the capture script run once against the real portal produces fixtures with no real identifiers (`rg` for each known value returns nothing).

- [ ] Phase 2: Config flow, coordinator, and one usage statistic on the local instance
  - Goal: the tracer bullet. A fresh local Home Assistant, configured through the flow, shows hourly kWh for the location on the Energy dashboard from the connect date forward.
  - Files: `config_flow.py`, `coordinator.py`, `writer.py`, `strings.json`, `translations/en.json`, `config/configuration.yaml`, `mise.toml` (`dev`, `dev:bootstrap`, `strings:check`), `scripts/dev_bootstrap.py`, `tests/test_config_flow.py`, `tests/test_writer.py`.
  - Work: the three-step user flow with the tariff step reduced to cycle day (rates arrive in Phase 3), verification poll before create; coordinator with the entry-owned listener; writer that reads seeds from `[floor, W)`, builds cumulative rows, diffs, and writes the total usage statistic from the connect date; `mise run dev` (ensure_config, symlink, `hass --debug`); `dev:bootstrap` headless onboarding and token; `strings:check`.
  - Validation: config-flow tests for user, duplicate, auth failure, poll failure; writer tests on the real recorder for idempotent re-import and a mid-window revision; on the local instance, `recorder/list_statistic_ids` shows the usage id and `statistics_during_period` returns the fixture's hours with sums; the Energy dashboard grid source configured with `stat_energy_from` renders bars.

- [ ] Phase 3: Tariff, cost, and per-period statistics
  - Goal: dollars on the dashboard. NiteFlex prices every cycle; cost and per-period usage statistics exist; rate versions are in entry data.
  - Files: `tariff/{__init__.py,base.py,niteflex.py}`, `config_flow.py` (tariff step with rate fields and `effective_from`), `writer.py`, `coordinator.py`, `tests/tariff/test_niteflex.py`, `tests/test_writer.py`.
  - Work: `Tariff` protocol and `RateVersion`, `Cycle`, `HourUsage`, `HourCost`; NiteFlex with chronological allowance tiering, per-hour smear over nominal cycle hours, PCA outside the allowance, tax on energy + fixed + rider, recurring adjustment untaxed; version selection by local day; cycle splitting by configured day with clamping and the partial first cycle; tariff preselection from rate schedule codes; per-period statistic ids from period slugs; cost written from the same rows.
  - Validation: table-driven tariff tests including the 13-day sample and an allowance-crossing cycle; tests that a past-dated version reprices only from its date; on the local instance, `stat_cost` set on the grid source renders dollar bars and the daily total for the sample period is $85.99 plus tax.

- [ ] Phase 4: Reconcile windows, revisions, and the reconcile action
  - Goal: bounded routine runs, full passes on the right triggers, self-healing gaps, and a manual repair path.
  - Files: `coordinator.py`, `writer.py`, `services.yaml`, `__init__.py` (service registration), `tests/test_reconcile.py`.
  - Work: routine window from the cycle containing `now - 7 days`; full-pass flag set on first run, config change, and the `reconcile` action; empty seed past the floor promotes to full; revision logging; `reconcile(config_entry_id, from)`.
  - Validation: tests for routine vs full window selection, gap promotion, a revision fixture changing exactly one usage row and the cost tail, and the action; on the local instance, deleting a mid-history row through the recorder and running the action restores it.

- [ ] Phase 5: Sensors, device, diagnostics, repairs
  - Goal: the entity surface and the two repair issues.
  - Files: `sensor.py`, `diagnostics.py`, `repairs.py` (if a fix flow is warranted), `strings.json`, `translations/en.json`, `tests/test_sensor.py`, `tests/test_diagnostics.py`, `tests/snapshots/`.
  - Work: the four sensors plus `newest_data_hour`, allowance sensors per tiered period, device info, `PARALLEL_UPDATES = 0`, diagnostics with key and value redaction, repair issues for unknown label and newer published version, `cost_skipped` handling.
  - Validation: Syrupy snapshots of states and diagnostics; a test that the diagnostics dump contains no fixture identifier; a test that polling continues with every entity disabled; on the local instance, the device page shows the sensors and the repair appears when a fixture with an unknown label is served.

- [ ] Phase 6: Reconfigure, reauth, options, and the true-up hook
  - Goal: every flow and the cycle record path.
  - Files: `config_flow.py`, `store.py`, `services.yaml`, `__init__.py`, `tests/test_config_flow.py`, `tests/test_store.py`, `tests/test_finalize.py`.
  - Work: reconfigure menu (add version, remove newest, cycle day, credentials) with `async_update_reload_and_abort` and full-pass triggers; reauth; options with poll interval; `CycleStore` v1; `finalize_cycle` and `unfinalize_cycle` with overrides, residual, read-date bounds, and full pass from the cycle start.
  - Validation: flow coverage for reauth, reconfigure, options, identity mismatch; a finalize test where the cycle's cost sum equals `bill_total` to the cent and the next cycle starts at `read_end`; unfinalize restores the estimate.

- [ ] Phase 7: CI, orbs, docs, first pre-release
  - Goal: the repository is installable through HACS and workable in an orb.
  - Files: `.github/workflows/ci.yml` (hassfest, HACS action, coverage), `.agents/setup`, `.agents/resume`, `.amp/services.yaml`, `mise.toml` (`release`), `README.md` (installation, removal, parameters, data updates, known limitations, troubleshooting), `DEV.md`, `custom_components/nisc_smarthub/brand/icon.png`.
  - Work: CI jobs; orb scripts (mise install, `uv sync --dev`, dev instance as a portal service, bootstrap on resume); release task bumping `manifest.json`, tagging, `gh release create --prerelease`; README per the Bronze and Silver documentation rules; inventory `hass`'s installed closure and decide whether to declare it now.
  - Validation: CI green on a PR; a fresh orb thread runs `mise run test` and opens the dev instance portal; `v0.1.0-rc.1` appears as a pre-release and HACS on the local instance (or a HACS-equipped local run) installs it from the custom repository.

- [ ] Phase 8: Appliance cutover runbook
  - Goal: a written, MCP-driven runbook for the next effort. Documented here, executed there.
  - Files: `docs/runbooks/appliance-cutover.md`.
  - Work: add the custom repository and download through `ha_manage_hacs`; restart; drive the config flow through `ha_set_integration`; verify statistics through `recorder/statistics_during_period` against the local numbers; repoint the "Cobb EMC" grid source's `stat_energy_from` and `stat_cost`; remove gagata's integration and clear its statistic ids through `recorder/clear_statistics` with the ids enumerated explicitly; record the first bill's actuals with `finalize_cycle` when it posts.
  - Validation: a dry read of the runbook against the MCP tool list; no execution in this effort.

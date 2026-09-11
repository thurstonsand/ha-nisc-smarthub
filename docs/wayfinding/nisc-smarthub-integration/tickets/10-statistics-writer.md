---
status: closed
type: grilling
claimed: 2b
blocked-by: [6]
---

# Statistics writer and reconcile window

## Question

How the integration turns poll responses into external statistics that stay correct across SmartHub revisions, rate changes, and bill-time true-ups.

Branches to settle, against [Home Assistant statistics import contract](06-ha-statistics-contract.md):

- Statistic ids and metadata for usage total, usage per period, and cost; how many hours of history the first run backfills (SmartHub's `connectDate` is the floor).
- The reconcile window: "start of the oldest unfinalized billing cycle to the newest hour SmartHub has" as the default; whether a smaller window is safe for the common case and the full one reserved for a repair action.
- Sum continuity: seeding each run from the last stored `sum` before the window, rewriting every hour from there forward, and never leaving a later point unrewritten.
- Revisions: SmartHub can change a past hour's kWh. gagata's re-imports from `last_stat - 2 days` and skips rows at or before its seed, so holes it misses are never backfilled; decide how ours detects and absorbs revisions and how far back it looks.
- Diff before write: whether to read existing points and write only changed hours, or rewrite the window unconditionally on every run.
- Polling cadence and offsets: SmartHub data lags about a day; six-hourly polling vs something tied to the portal's update rhythm.
- The true-up hook: how a finalized cycle is represented (a stored record per cycle with the bill's actual values), how the writer reprices that cycle exactly once, and how the window then advances past it. The bill reader itself is fog.
- A `full` reconcile service action as the repair path, and what it clears first.
- Failure classes: `ConfigEntryAuthFailed` on TOTP/password failure, `UpdateFailed` on `PENDING` timeouts, and what a partial poll (one series missing) does.

Cost of being wrong: a bad window or seam corrupts the recorder's sums, which the dashboard renders as spikes; recovering means clearing and re-importing history.

## Resolution

Grilled in one round on 2026-09-11 against [ha-statistics-contract](../research/ha-statistics-contract.md) and [ha-integration-practice](../research/ha-integration-practice.md). The contract:

**Invariant.** Every write ends at the newest hour SmartHub has. A window never stops short, so the tail replay the recorder needs after a changed cumulative offset is inherent, not a special case.

**Statistics.** `nisc_smarthub:<account>_<location>_usage`, `..._usage_<period_slug>` (one per tariff period when the tariff has more than one), `..._cost`. Usage: `unit_class="energy"`, kWh. Cost: `unit_class=None`, `"USD"`. All `has_sum=True`, `mean_type=NONE`. Rows carry `state` (the hour's value) and `sum` (cumulative from the history floor). The history floor is the account's connect date from the `billing` endpoint.

**Two windows.** Routine, every poll: from the start of the cycle containing `now - 7 days` to now, so at most two cycles. Full: from the oldest unfinalized cycle's start (or the floor) to now, triggered by the first run, a rate version added or removed, a cycle-day change, a cycle finalized or unfinalized, or the `reconcile` action. Every event that can change a past hour's price triggers the full pass.

**Seed.** The running sum for a window starting at W is the last row of an hourly range read of `[floor, W)` per statistic id, via `statistics_during_period` in the recorder executor; 0 when W is the floor. An empty read with W past the floor means a hole: the run promotes itself to a full reconcile from the floor rather than seeding 0 or failing.

**Diff before write.** The window's existing rows are read back; only rows whose `state` or `sum` differ beyond float tolerance are written, and each revised hour is logged with old and new kWh. Revision detection is the purpose; the write savings are incidental.

**Missing hours.** An hour absent from SmartHub's response gets no row. Smear and tiering use the cycle's nominal hour count from the calendar, so the absent hour's share of fixed charges is simply missing until the hour arrives. Hours newer than the last written point that have not appeared are lag, not error.

**Cycle records** live in a versioned `helpers.storage.Store` per entry (`.storage/nisc_smarthub.<entry_id>`): config in the entry, observed facts in the store.

**True-up hook.** `nisc_smarthub.finalize_cycle(config_entry_id, cycle_start, read_start, read_end, service_charge, pca_factor, tax, bill_total)` writes the cycle record and triggers a full reconcile from that cycle's start; read dates replace the calendar bounds, which can shift the next unfinalized cycle's start. `nisc_smarthub.unfinalize_cycle(config_entry_id, cycle_start)` reverses it. A future bill reader is a second caller of the same function.

**Reconcile action.** `nisc_smarthub.reconcile(config_entry_id, from: date = floor)`. Never clears rows; upsert overwrites everything from `from`.

**Polling.** A `DataUpdateCoordinator` with an entry-owned listener registered in `async_setup_entry`, so polling continues with zero entities. Default interval six hours, configurable.

**Failures.** 401, or the 500 "could not be verified" body: `ConfigEntryAuthFailed`. Poll still `PENDING` after 5 retries at 4 s, or any other HTTP or parse error: `UpdateFailed`. `USAGE` present but `TIME_OF_USE` missing or with an unknown label: usage written, cost skipped, repair issue (ticket 09).

**Test seam.** The real recorder through `pytest-homeassistant-custom-component` (`recorder_mock`, `async_wait_recording_done`), reading back with the production `statistics_during_period` signature: idempotent re-import, a mid-window revision producing exactly one changed usage row and a changed cost tail, gap-promotes-to-full, UTC conversion across a DST transition, and independent usage and cost seeds.

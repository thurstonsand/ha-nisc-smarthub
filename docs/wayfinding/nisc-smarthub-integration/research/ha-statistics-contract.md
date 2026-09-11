# Home Assistant statistics import contract

## Rules we must obey

- Write hourly external statistics with `async_add_external_statistics`, on the event loop, using `source="nisc_smarthub"` and valid `nisc_smarthub:<name>` ids. The call queues work; it does not await a database commit. [Import implementation][import]
- Supply all seven required metadata fields, including `mean_type=StatisticMeanType.NONE` and `has_sum=True`. Use `unit_class="energy", unit_of_measurement="kWh"` for usage and `unit_class=None, unit_of_measurement="USD"` for our cost statistic. The type contract requires both new fields, although 2026.9.1 still supplies deprecated runtime defaults. [Types][types], [validation][import], [Opower metadata][opower]
- Supply timezone-aware, top-of-hour starts, preferably already in UTC, and concrete reusable lists of points. Imports normalize and mutate timestamps, then iterate the same collection again in the recorder. [Import implementation][import]
- Write each interval's ending cumulative total as `sum`. Re-import replaces the selected row's `state` and `sum`, but never fixes later sums. Carry any net correction forward through the stored tail, and seed from strictly before the first rewritten hour. [Replacement][update], [change calculation][change]
- Read hourly rows in the recorder executor, not the event loop. A read of the newest row is not a historical seed query, and queued imports must finish before dependent reads. [Range API][range], [Opower][opower], [queue API][queue]
- Connect Energy's `stat_energy_from` to usage and `stat_cost` to cost; do not configure a fixed or entity price for an external usage statistic without an explicit cost statistic. Clear only explicitly selected ids through the recorder queue, understanding that clear removes their entire history and metadata. [Energy schema][energy-schema], [clear API][clear], [deletion][delete]

## Source version and scope

All Core references below resolve to tag `2026.9.1`, commit `fc034572d0216a04ed40a07154394908a594dfed`, inspected in a shallow sparse checkout. The test-package README is its current primary documentation, which identifies Core 2026.9.1 at the time of inspection. This is source research, not a production experiment or an executed Core test run. The dashboard findings cover the Core configuration, validation, and statistics transport contract. Exact browser card query selection cannot be verified from Core alone; the allowed sources exclude `home-assistant/frontend`.

## Import APIs and metadata types

Both public Python entry points have this signature, with the same parameter types and `None` return: `async_add_external_statistics(hass: HomeAssistant, metadata: StatisticMetaData, statistics: Iterable[StatisticData], *, _called_from_ws_api: bool = False) -> None`, and `async_import_statistics(...) -> None`. Despite their names they are synchronous `@callback` functions for the event loop, not coroutines. The former accepts external ids, while the latter requires an entity id and `source="recorder"`. Do not use the websocket-only flag from an integration. Both enter `_async_import_statistics`, which validates and queues an import directly into `Statistics`, the hourly table. [Import implementation][import]

`StatisticMetaData` is a `TypedDict` with these required keys; nullable means the key is present with a `None` value, not omitted. [Type declarations][types]

| Key | Python type | Usage value | Cost value |
| --- | --- | --- | --- |
| `mean_type` | `StatisticMeanType` | `StatisticMeanType.NONE` | `StatisticMeanType.NONE` |
| `has_sum` | `bool` | `True` | `True` |
| `name` | `str \| None` | Human-readable name | Human-readable name |
| `source` | `str` | `"nisc_smarthub"` | `"nisc_smarthub"` |
| `statistic_id` | `str` | `"nisc_smarthub:<usage_name>"` | `"nisc_smarthub:<cost_name>"` |
| `unit_class` | `str \| None` | `"energy"` | `None` |
| `unit_of_measurement` | `str \| None` | `"kWh"` | `"USD"` |

`has_mean: NotRequired[bool]` is an additional optional key retained only for compatibility. `StatisticMeanType` is an `IntEnum`: `NONE=0`, `ARITHMETIC=1`, `CIRCULAR=2`. `StatisticData` requires `start: datetime`; its optional numeric keys `state`, `sum`, `min`, `max`, `mean`, and `mean_weight` are typed `float`, and optional `last_reset` is `datetime | None`. There is no imported `change` field. [Type declarations][types]

The 2026.9.1 runtime still fills missing `mean_type` from deprecated `has_mean`, defaulting to `NONE`, and infers missing `unit_class` from the unit or sets it to `None`. Public imports report deprecation with a `2026.11` break version. The [2025-10-16 developer blog](https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/) still says Python `unit_class` omission stops in 2025.11, but the pinned implementation contradicts that date. Supply the fields regardless. The websocket schema also still makes them optional, coerces numeric mean types to the enum, and warns about 2026.11. [Import implementation][import], [websocket import][ws-import]

## Validation and ownership

External ids match `^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$`: lowercase ASCII letters, digits, and underscores on both sides of one colon, no leading/trailing underscore, and no double underscore. Metadata `source` must be nonempty and equal the prefix. Contrary to the earlier research, the external entry point does **not** explicitly prohibit the prefix `recorder`; our integration must use its own domain as an ownership convention. [Id validator][ids], [external import][import]

For non-null `unit_class`, Core requires a known converter and a unit in that converter's `VALID_UNITS`; invalid combinations raise `HomeAssistantError`. For `unit_class=None`, this import path does not validate the unit against a currency list or the configured currency. Thus `USD` is our deliberate unit declaration, not an import-enforced currency match. Opower supplies the precedent for electric energy's converter and kWh, but uses **no unit at all** for its costs. [Validation][import], [Opower][opower]

Every point's start must have a usable timezone offset and zero minute, second, and microsecond fields. Core then changes it to UTC in place. A non-null `last_reset` must also be timezone-aware and is normalized. Top-of-hour validation occurs before normalization, so use UTC at the integration boundary rather than relying on offsets that are not whole hours. This Python path does not comprehensively validate value types, finite numbers, chronology, uniqueness within a batch, or the relationship between `has_sum` and point values. Our writer must enforce those invariants. A generator is unsuitable despite the `Iterable` annotation: validation consumes it before the queued writer iterates it. [Import implementation][import]

## Replacement and cumulative continuity

The recorder resolves metadata to a metadata id, searches for an existing row with that id and the exact start timestamp, then updates that row or inserts one. It does not merge a historical batch with later cumulative totals. On update, `mean`, `min`, `max`, `last_reset_ts`, `state`, and `sum` are set from the supplied point; omitted fields become SQL null through `.get()`. `mean_weight` is notably absent from this update dictionary, so this is not a universal replacement of every possible statistic field. Always supply both `state` and `sum` for our sum statistics. [Timestamp lookup][upsert], [import loop](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L2903-L2929), [update dictionary][update]

Re-import can also update metadata's mean type, sum flag, name, unit class, and unit label for the whole statistic. Changing the unit label here does not convert untouched historical numbers; keep units stable or use a deliberate migration. [Metadata update](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/table_managers/statistics_meta.py#L195-L248)

`sum` is stored data, not a running total that the import API computes. `change` is a read-time subtraction: Core obtains the last sum strictly before the requested start, defaults the seed to zero if unavailable, then emits each returned sum minus the preceding sum. A null sum produces a null change without advancing the seed. Aggregated day/week/month/year reads reduce the rows before this subtraction. [Change calculation][change], [range reduction][range], [strict prior lookup][prior]

For example, stored sums `10, 15, 20` yield changes `10, 5, 5` with no earlier seed. Correcting only the middle sum to `17` produces `10, 7, 3`; correcting the tail to `22` preserves the intended final change of `5`. This arithmetic follows directly from the read implementation. A correction need not always create a large spike, but it creates an erroneous compensating delta at the boundary whenever the net cumulative correction is nonzero. Recompute through the newest stored point unless the cumulative offset has already returned to zero. [Change calculation][change]

Core also has an additive sum-adjustment path that changes sums at and after a timestamp without repricing interval states. It is useful context, not a substitute for our deterministic replay of usage and cost. [Adjustment implementation](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L3019-L3058)

## Range reads, seeds, and execution context

The full range signature is `statistics_during_period(hass, start_time: datetime, end_time: datetime | None, statistic_ids: set[str] | None, period: Literal["5minute", "day", "hour", "week", "month", "year"], units: dict[str, str] | None, types: set[Literal["change", "last_reset", "max", "mean", "min", "state", "sum"]]) -> dict[str, list[StatisticsRow]]`. All arguments are required in the production function. Use `await get_instance(hass).async_add_executor_job(statistics_during_period, hass, start, end, ids, "hour", None, {"state", "sum"})`, following Opower. For hourly queries the SQL interval is `[start, end)`, rows are ordered by metadata id then ascending start, and absent data returns `{}`. Day and larger periods align bounds to local calendar boundaries; do not use those periods for reconcile seeds. [Range API][range], [query bounds][bounds], [Opower][opower]

`get_last_statistics(hass, number_of_stats: int, statistic_id: str, convert_units: bool, types: set[Literal["last_reset", "max", "mean", "min", "state", "sum"]])` reads the latest hourly rows, selecting them in descending timestamp order. It has **no before-time argument**. `get_last_statistics(hass, 1, id, False, {"sum"})` is suitable for an append-only seed, not a historical rewrite. [Latest API][latest]

For a rewrite beginning at `W`, a public-function solution is an hourly range read from a known history lower bound through exclusive end `W`, then taking the final returned sum independently for each id. A query of just `[W-1h, W)` is sufficient only when continuity is already established. If it is empty, widen backwards to the known coverage boundary; do not interpret a gap as a zero seed. Core's private `_statistics_at_time` already selects the newest row strictly before a cutoff, but requires a recorder session, metadata ids, and a table. Avoid coupling the integration to that private helper unless the design explicitly accepts it. [Bounds][bounds], [private lookup][prior]

Python read timestamps are Unix seconds; `recorder/statistics_during_period` converts start/end and non-null last-reset timestamps to milliseconds for websocket clients. Its handler moves the query to the recorder executor. Import success means validation and enqueueing succeeded, not that the write committed. Do not mutate metadata or lists after handing them to the queue, and serialize dependent reconcile work rather than treating an executor read as a queue-drain barrier. [Websocket range][ws-range], [queue implementation][queue]

## Clearing, deletion, and migration

From an integration, call `get_instance(hass).async_clear_statistics(statistic_ids: list[str], *, on_done: Callable[[], None] | None = None)`. This enqueues a `ClearStatisticsTask`. The lower-level `clear_statistics(instance: Recorder, statistic_ids: list[str])` calls the metadata manager, which asserts that it runs on the recorder thread; do not invoke it in a general executor. [Queue API][queue], [clear implementation][clear], [metadata deletion][delete]

Clear deletes the selected metadata rows, clears their metadata cache entries, and foreign-key cascades remove both hourly and short-term rows. It is not a date-range delete and does not delete raw entity states or edit Energy preferences. It can delete any selected statistic, not only external statistics, so a migration must enumerate only explicitly approved old ids. Stop their old writer before clearing, or it can import them again. A full reconcile can clear our selected ids and recreate all history, but this is a destructive rebuild rather than an atomic swap. [Metadata deletion][delete], [foreign keys][schema], [queue implementation][queue]

The admin-only websocket command is `{"type":"recorder/clear_statistics","statistic_ids":["nisc_smarthub:example"]}`. Despite its stale docstring saying it returns without waiting, the 2026.9.1 handler supplies an `on_done` callback, waits on an event with a timeout, and only then returns success. Mirror its thread-safe callback handoff if awaiting completion inside an integration. [Websocket clear][ws-clear]

## Energy dashboard contract and hourly materialization

Grid preferences hold `stat_energy_from` and `stat_cost` as statistic-id strings. The schema rejects entity or nonzero numeric prices on external usage when no explicit cost statistic is supplied; if `stat_cost` is set, it permits those fields because the explicit statistic takes precedence. Energy's generated cost sensor path only supports entity-backed energy, not arbitrary external ids. [Grid schema][energy-schema], [sensor implementation](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/energy/sensor.py#L170-L260)

The earlier research overstates `_async_validate_cost_stat`: for an external id it checks existence in metadata and returns immediately. It does **not** require `has_sum` or the configured currency there. Usage validation likewise stops after the common external-statistic existence check. We must still provide usable sum data and correct units ourselves; weak validation is not a useful design target. [Cost validation][energy-validate], [usage validation](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/energy/validate.py#L190-L291)

External imports write `Statistics` directly, so their hourly data becomes available when the queued import commits. They do not create five-minute rows and need no later hourly materialization. `statistics_during_period` selects `StatisticsShortTerm` only for `period="5minute"`, and `Statistics` otherwise. Entity-derived statistics follow a different path: the compiler summarizes the hour's five-minute rows, taking its final `state`, `sum`, and `last_reset`, when processing the interval starting at minute 55. [Import target][import], [table selection][range], [hourly compiler](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L546-L609), [compiler scheduling](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L804-L838)

The Core transport exposes sum and derived change for both ids through the same recorder websocket API; there is no separate cost-statistics table. The Core Energy fossil-consumption endpoint itself demonstrates requesting `change` through that statistics API. The browser's exact per-card period/type selection remains outside this source scope, so no claim here depends on a frontend `dev` checkout. [Websocket statistics][ws-range], [Energy consumer](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/energy/websocket_api.py#L240-L299)

A search of the pinned Core Energy component finds `cost_adjustment_day` in preference types, schema, and legacy grid migration, not cost calculation. This supports the existing finding that Core does not apply it as a service charge. It does not independently prove the absence of a browser-side calculation, which would require frontend sources. [Schema][energy-schema], [migration](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/energy/data.py#L631-L675)

## Opower precedent

Opower sanitizes a utility/meter/account-derived id prefix and writes four external series: consumption, return, cost, and compensation. All use `mean_type=NONE`, `has_sum=True`, and `source="opower"`. Electric consumption and return use the energy converter and kWh. Crucially, cost and compensation use **both `unit_class=None` and `unit_of_measurement=None`**, not USD. Our explicit USD unit is appropriate to our tariff, but it is a deliberate difference from this precedent. [Coordinator][opower]

On first import Opower seeds all four sums with `0.0`. On subsequent updates it reads the latest consumption row, fetches overlapping utility reads, then queries statistics at the first returned read's start using `[start, start+1 second)`. If that yields nothing, it queries from that start onward and uses the oldest returned point. It seeds sums from that anchor, tolerates missing companion series with zero, and skips incoming reads at or before the anchor. This is append-after-anchor behavior, **not** a prior-window seed and full historical repricing algorithm. [Coordinator][opower]

For each later utility interval, Opower separates positive consumption/cost from return/compensation, adds each interval value to a running sum, and writes `StatisticData(start=start, state=interval_value, sum=running_sum)` through the external API. This is the model for our row shape and executor usage, not proof that its correction policy meets our true-up requirements. [Coordinator][opower]

## Testing with pytest-homeassistant-custom-component

The [package README](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/master/README.md) says it extracts Core's test support, updates daily against released HA versions including betas, and exposes ordinary fixtures such as `hass`. Pin a package version generated for our Core target rather than tracking latest blindly. Enable `enable_custom_integrations`, arrange `recorder_mock` before it where needed, and configure `asyncio_mode = auto`. The generated [recorder helper module](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/blob/master/src/pytest_homeassistant_custom_component/components/recorder/common.py) exposes `async_wait_recording_done` under `pytest_homeassistant_custom_component.components.recorder.common`.

Core's `test_import_statistics` uses `recorder_mock`, calls the real import function with metadata and a tuple of points, awaits `async_wait_recording_done(hass)`, reads hourly rows, asserts metadata and latest sums, then re-imports the same timestamp and checks that only that row's values change. It parameterizes external and recorder-owned imports, old/new metadata, and timezone-aware last-reset values. Its adjacent error tests exercise invalid ids/sources and timestamp failures. This is stronger evidence than a mocked assertion that our code called the writer. [Core import test](https://github.com/home-assistant/core/blob/2026.9.1/tests/components/recorder/test_statistics.py#L860-L1137), [validation tests](https://github.com/home-assistant/core/blob/2026.9.1/tests/components/recorder/test_statistics.py#L1140-L1312)

`async_wait_recording_done` drains HA tasks, triggers a database commit, waits for recorder completion, then drains HA tasks again. A bare `hass.async_block_till_done()` is not its equivalent. Also, Core tests import a convenience `statistics_during_period` wrapper with defaults from recorder test helpers; those defaults are absent from the production API. Integration tests should exercise the real range API through the recorder executor with all arguments. [Test barriers and wrapper](https://github.com/home-assistant/core/blob/2026.9.1/tests/components/recorder/common.py#L125-L217)

The proposed writer's regression suite should extend that pattern with repeated-import idempotence, omitted-field nulling, explicit kWh/USD metadata, no five-minute rows, a historical correction followed by tail replay with `types={"sum", "change"}`, empty versus gapped seed history, UTC conversion across local DST transitions, independent usage/cost seeds, and clearing only designated ids. These are design recommendations derived from the verified contracts above, not tests claimed to exist or have run in this repository.

## Corrections to the earlier cost research

[ha-cost-constraints.md](ha-cost-constraints.md) correctly identifies external ids, timestamp validation, cumulative storage, upsert behavior, and the explicit `stat_cost` path. Four claims need correction or qualification:

1. `unit_class` and `mean_type` are required by the typed metadata contract, but omission still works with deprecation handling in 2026.9.1. The code targets 2026.11, contradicting the blog's Python `unit_class` deadline of 2025.11. [Implementation][import]
2. External source validation requires a matching prefix but does not expressly reject `source="recorder"`. Our own-domain rule is intentional, not an additional check in this API. [Implementation][import]
3. `_async_validate_cost_stat` does not enforce a sum or configured currency for external statistics; it only checks metadata existence before returning. Opower costs are unitless, not USD. [Validation][energy-validate], [Opower][opower]
4. Tail correction is necessary when a rewrite changes the continuing cumulative offset, not literally for every possible rewrite. The visible artifact is a compensating error at the first unchanged sum, which may or may not look like a spike. The `cost_adjustment_day` conclusion is supported for Core, but the stronger frontend conclusion is not reverified under this ticket's allowed-source restriction. [Change calculation][change], [Energy schema][energy-schema]

[types]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/models/statistics.py#L18-L74
[import]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L2769-L2900
[ids]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L458-L471
[update]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L880-L926
[upsert]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L2753-L2766
[change]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L2036-L2090
[range]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L2093-L2251
[bounds]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L1459-L1479
[latest]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L2254-L2342
[prior]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L2445-L2569
[queue]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/core.py#L556-L616
[clear]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/statistics.py#L970-L974
[delete]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/table_managers/statistics_meta.py#L399-L410
[schema]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/db_schema.py#L627-L754
[ws-clear]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/websocket_api.py#L370-L404
[ws-import]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/websocket_api.py#L566-L624
[ws-range]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/recorder/websocket_api.py#L199-L289
[energy-schema]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/energy/data.py#L268-L486
[energy-validate]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/energy/validate.py#L355-L396
[opower]: https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/components/opower/coordinator.py#L149-L389

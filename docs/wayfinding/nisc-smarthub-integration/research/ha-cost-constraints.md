# Home Assistant cost constraints

Why the Energy dashboard will not price gagata's usage statistic, what path remains, and the rules any statistics writer inherits. Read against `home-assistant/core` `dev` on 2026-09-11 (the appliance runs 2026.9.1; the cited functions are unchanged there) and the frontend `dev` branch. The statistics import contract itself is the subject of a separate research ticket; this note records only what the charting session established.

## The refusal

`homeassistant/components/energy/data.py`, `GRID_SOURCE_SCHEMA` (flat since the 2026 grid-source migration; the old `flow_from`/`flow_to` arrays are converted on load), runs `_reject_price_for_external_stat(stat_key="stat_energy_from")`:

```python
if stat_id is not None and not valid_entity_id(stat_id):
    if val.get(cost_stat_key) is not None:
        return val          # a cost stat is set; price fields are ignored anyway
    if entity_price or number_price:
        raise vol.Invalid("Entity or number price is not supported for external statistics. Use stat_cost instead")
```

`smarthub:smarthub_energy_sensor_...` is not a valid entity id, so `number_energy_price` and `entity_energy_price` are rejected. Confirmed live: `energy/save_prefs` with a price on the grid source fails validation; with `stat_cost: null` and no price it saves.

The reason is in `homeassistant/components/energy/sensor.py`: an `EnergyCostSensor` is created only when `valid_entity_id(stat_energy)` passes, and it computes cost by `async_track_state_change_event` on that energy entity, multiplying each increment by the price as state changes arrive. An external statistic has no entity and no state changes, so there is nothing to subscribe to. HA refuses the configuration rather than accepting it and silently producing nothing.

## The path that remains

`stat_cost` on the grid source may point at any statistic id, including an external one (`_async_validate_cost_stat` only checks that metadata exists for the id; it does not verify `has_sum` or the currency, per [ha-statistics-contract.md](ha-statistics-contract.md)). So the integration writes its own USD external statistic and the dashboard reads it. Core's `opower` integration does exactly this for utilities that report cost (`homeassistant/components/opower/coordinator.py`, `_async_update_data` and `_insert_statistics`), and is the first-party pattern to copy for metadata.

Two entries per grid source matter: `stat_energy_from` (kWh) and `stat_cost` (USD). Both must exist as sum statistics with hourly points.

## `cost_adjustment_day` is dead

The grid-source field `cost_adjustment_day` looks like a standing-charge hook and was proposed as the home for the $33 service charge. It is not. In core it appears only in the schema (`vol.Required("cost_adjustment_day"): vol.Coerce(float)`) and the legacy migration. In the frontend (sparse checkout of `src/data`, `src/panels/lovelace/cards/energy`, `src/panels/config/energy`) it appears only as a type field, a default of `0`, and a passthrough in `dialog-energy-grid-settings.ts`. Nothing reads it into a cost. Setting it changes no number on any card. Fixed charges therefore go into the cost statistic or nowhere.

## Rules for writing statistics (established so far)

- External statistic ids are `<source>:<name>`; `source` in the metadata must equal the prefix and must not be `recorder`.
- Imports upsert on `(metadata_id, start)`; `start` must be tz-aware and on the hour.
- Metadata `unit_class` and `mean_type` are mandatory (developer blog, 2025-10-16; not specifying `unit_class` stopped working in 2025.11). For kWh, `unit_class="energy"`; for a currency there is no converter, so `unit_class=None`.
- HA stores cumulative `sum` per point and derives the displayed `change` by differencing consecutive points. Rewriting a past hour without rewriting every later hour leaves the sums offset; the first unrewritten point absorbs the whole correction as a spike. Any rewrite therefore extends forward to the newest point, and each run seeds its running sum from the last stored point before the window.
- `recorder/statistics_during_period` over the websocket (or `statistics_during_period` in the recorder executor from inside HA) reads points back; `types: ["change"]` returns the per-period delta.

## Current appliance state

- HA 2026.9.1, currency USD, timezone America/New_York.
- Energy prefs: one grid source, `stat_energy_from = smarthub:smarthub_energy_sensor_<acct>_<loc>` (gagata's hourly usage), `stat_cost = null`, `cost_adjustment_day = 0`, named "Cobb EMC".
- Statistics present from gagata's: hourly and daily usage (`has_sum`, kWh, `unit_class = energy`), plus the recorder statistic of its month-to-date entity.

## Tooling note

The `pi-mcp-adapter` output guard elides tool results above 16 KB inside `mcpScript` (50 KB for a direct `mcp` call) and writes the full payload to a temp file it names in the result. Reading more than about five days of hourly statistics through the script tool needs chunked queries or the spill file.

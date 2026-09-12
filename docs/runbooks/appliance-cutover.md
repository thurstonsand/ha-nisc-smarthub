# Appliance cutover: from gagata's `smarthub` to `nisc_smarthub`

The production Home Assistant is an appliance. Nothing is installed on it by hand; every step below goes through the `home-assistant` MCP server, its REST proxy, or its WebSocket commands. This runbook is written before the cutover; steps marked *unverified* have not been exercised against the appliance and their call shapes come from the tool descriptions, not from a run.

Never write credentials into this file, a chat, or a log. They live in 1Password item `4l6swgjzetxapzc65weiadfdwi` in the `agent` vault, section `SmartHub`.

## State before

- Grid source on the Energy dashboard: `stat_energy_from: smarthub:smarthub_energy_sensor_<account>_<location>`, name "Cobb EMC", `cost_adjustment_day: 0`, no cost statistic.
- gagata's integration: domain `smarthub`, config entry `01M2765E85ATQMTWXKJXMFEPE8`, installed through HACS as a custom repository.
- Its statistics: `smarthub:smarthub_energy_sensor_<account>_<location>` (hourly) and probably `smarthub:smarthub_energy_sensor_daily_<account>_<location>`. gagata's ids embed the account number and the location id, which is why they are never spelled out here; enumerate them on the appliance before touching them.

## 0. Publish a pre-release

On the laptop, on a clean `main`:

```sh
mise run release 0.1.0-rc.1 --dry-run
mise run release 0.1.0-rc.1
```

Verify: `gh release view v0.1.0-rc.1` shows a pre-release whose `manifest.json` at that tag says `0.1.0-rc.1`.

## 1. Install through HACS

Through MCP (`ha_manage_hacs`), add `https://github.com/thurstonsand/ha-nisc-smarthub` as a custom repository of category integration, then download it. HACS offers a pre-release only when the repository is set to show them; set that on the repository entry first. *Unverified*: the exact argument names of `ha_manage_hacs` for "add custom repository" and "show pre-releases"; read the tool description at the time.

Restart with `ha_restart confirm: true` (about 75 s).

Verify: `GET /api/config/config_entries/flow_handlers` (or the MCP integration listing) includes `nisc_smarthub`.

## 2. Configure the entry

Through MCP (`ha_set_integration`) or the REST flow API (`POST /api/config/config_entries/flow` with `{"handler": "nisc_smarthub"}`, then `POST /api/config/config_entries/flow/<flow_id>` per step):

1. `user`: host, email, password, TOTP secret. Read them from 1Password at the moment of the call; never echo them. A wrong TOTP secret counts as a failed login, and a few failed logins lock the SmartHub account for about forty minutes.
2. `location`: appears only when the login sees more than one service location.
3. `cycle`: tariff `niteflex` (preselected from the codes `NFON`, `NFOFF`, `NFSOF`), the seeded 2026-01-01 rates, cycle day 28.

Verify: the flow returns `create_entry`; the entry state is `loaded`; the debug log shows `reconciling a full window` from 2026-08-28.

## 3. Verify the statistics

Over the WebSocket API:

```json
{"type": "recorder/list_statistic_ids", "statistic_type": "sum"}
```

Expect five ids with the `nisc_smarthub:` prefix: `_usage`, `_usage_on_peak`, `_usage_off_peak`, `_usage_super_off_peak`, `_cost`.

```json
{"type": "recorder/statistics_during_period", "start_time": "2026-09-01T04:00:00+00:00", "end_time": "2026-09-04T04:00:00+00:00", "statistic_ids": ["nisc_smarthub:<account>_<location>_usage"], "period": "hour", "types": ["state", "sum"]}
```

Expect 72 rows whose states sum to 157.55 kWh, the number the local instance and the fixtures agree on. Chunk reads at five days or fewer; the MCP output guard truncates larger results.

Check `repairs/list_issues`: no `nisc_smarthub` issue is expected on the appliance (the connect-day and tail unclassified hours are logged, not raised).

## 4. Repoint the Energy dashboard

Read the current preferences with `energy/get_prefs`, then save them back with the grid source changed. The schema is flat and strict: `cost_adjustment_day` is required, `name` and `stat_power` must be omitted rather than sent as `null`.

```json
{"type": "energy/save_prefs", "energy_sources": [{"type": "grid", "stat_energy_from": "nisc_smarthub:<account>_<location>_usage", "stat_energy_to": null, "stat_cost": "nisc_smarthub:<account>_<location>_cost", "entity_energy_price": null, "number_energy_price": null, "stat_compensation": null, "entity_energy_price_export": null, "number_energy_price_export": null, "cost_adjustment_day": 0}], "device_consumption": []}
```

Keep every other source and device from `energy/get_prefs` in the payload; `save_prefs` replaces the whole object.

Verify: `energy/validate` returns no issues; the dashboard shows kWh and dollar bars from 2026-08-28.

## 5. Remove gagata's integration

1. `DELETE /api/config/config_entries/entry/01M2765E85ATQMTWXKJXMFEPE8`.
2. Enumerate its statistics: `recorder/list_statistic_ids` filtered on the `smarthub:` prefix. Expect the hourly id and possibly the daily one, nothing else.
3. Clear exactly those ids:

    ```json
    {"type": "recorder/clear_statistics", "statistic_ids": ["smarthub:smarthub_energy_sensor_<account>_<location>", "smarthub:smarthub_energy_sensor_daily_<account>_<location>"]}
    ```

4. Remove the `gagata/ha-smarthub-energy-sensor` repository from HACS (*unverified* argument shape).

Verify: `recorder/list_statistic_ids` shows no `smarthub:` id; `energy/validate` is still clean.

## 6. The first bill (about October 2026)

When the first Cobb EMC bill posts, run the true-up with the bill's own values:

```yaml
action: nisc_smarthub.finalize_cycle
data:
  config_entry_id: <entry id>
  cycle_start: "2026-08-28"
  read_start: <the bill's service-from date>
  read_end: <the bill's service-to date>
  service_charge: 33.00
  pca_factor: <the PCA rate on the bill, $/kWh, signed>
  tax: <the sales tax line>
  bill_total: <the bill's electric total, Round Up excluded>
```

Verify: the cost statistic's sum over `[read_start, read_end)` equals `bill_total`; the entry's diagnostics show the record with its residual. A residual of more than a few dollars means a line the tariff does not model (the $30 establishment fee on a first bill is the likely one); note it in the wayfinding map's fog and, if it recurs, in the tariff.

If the bill's read dates differ from the guessed cycle day, consider changing the cycle day through Reconfigure so future cycles line up.

## Rollback

1. Re-add `gagata/ha-smarthub-energy-sensor` through HACS and restart.
2. Re-create its entry through the flow with the same credentials.
3. Repoint the grid source's `stat_energy_from` back to `smarthub:smarthub_energy_sensor_<account>_<location>` and drop `stat_cost`.
4. Leave the `nisc_smarthub:` statistics in place; they are harmless and a later cutover reuses them.

Its history refills from `last_stat - 2 days` on each run, so a gap between the cleared statistics and the re-created entry stays unless its integration is coaxed into a full re-import.

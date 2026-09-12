# DEV.md

## Setup

`mise trust` then enter the directory. The `enter` hook syncs the uv venv and installs hk's pre-commit hooks. The venv carries Home Assistant 2026.9.1, its test plugin, and the runtime closure of the dev configuration, so a fresh clone runs the instance without `hass` installing anything for itself.

## Commands

```sh
mise run check             # every non-mutating check: markdown, ruff format, ruff lint, basedpyright, strings
mise run fix               # every formatter and autofixer
mise run test              # pytest with coverage, gated at 95% of custom_components
mise run dev               # run Home Assistant against this checkout, in the foreground
mise run dev:bootstrap     # onboard a fresh dev instance and mint an agent token
mise run strings:check     # strings.json and translations/en.json must agree
mise run fixtures:capture  # re-record tests/fixtures from the live portal (laptop only)
mise run release 0.1.0     # bump the manifest, tag, push, publish the GitHub release
```

## Layout

```text
custom_components/nisc_smarthub/
  __init__.py        entry setup, the three service actions
  config_flow.py     user, reauth, reconfigure (menu), and options flows
  coordinator.py     one poll per run, window planning, pricing, repairs
  writer.py          external statistics: seeds, cumulative rows, diff, zeroed stale hours
  cycles.py          billing cycle boundaries, calendar and read-date driven
  store.py           finalized-cycle records in .storage/nisc_smarthub.<entry_id>
  sensor.py          the five sensor kinds; entity.py holds the device
  diagnostics.py     the redacted dump
  tariff/            the Tariff contract (base.py) and NiteFlex (niteflex.py)
  smarthub/          the portal client, free of Home Assistant imports
  strings.json       authored; translations/en.json is its mirror
  services.yaml      action schemas
scripts/             capture_fixtures, dev_bootstrap, check_strings, release
tests/               pytest suite; fixtures/ are scrubbed portal recordings; snapshots/ are Syrupy
docs/designs/        accepted design docs, amended in place with dates
docs/runbooks/       the appliance cutover
docs/wayfinding/     the planning map for this effort
config/              local dev instance; only configuration.yaml is tracked
.agents/, .amp/      Amp orb setup, resume, and the dev instance as a portal service
```

## The local instance

`mise run dev` creates the `config/custom_components -> ../custom_components` symlink if it is missing, refuses to touch anything else sitting at that path, and runs `hass --config config --debug --skip-pip` in the foreground. Restart after any Python, manifest, or translation change: reloading the entry does not reimport the modules.

`--skip-pip` works because the dev group in `pyproject.toml` declares the runtime closure of `default_config` at the versions 2026.9.1 pins, the frontend included. After a Home Assistant bump, regenerate that list: start the instance once without `--skip-pip`, then diff `uv pip freeze` against `uv.lock` and pin what `hass` installed. A `uv sync` that prunes something the instance needs shows up as recovery mode at the next start, where custom integrations never load.

With the instance running, `mise run dev:bootstrap` creates the owner `dev` / `development` over the onboarding API and writes a long-lived token to `config/.agent-token`. It is a no-op once that file exists. Inspect the instance with that token over REST (`/api/config`, `/api/config/config_entries/entry`) or the WebSocket API (`recorder/list_statistic_ids`, `recorder/statistics_during_period`, `energy/save_prefs`). The onboarding API is internal; when a Home Assistant upgrade breaks `scripts/dev_bootstrap.py`, fix the script.

Set the instance's timezone to the portal's before configuring the entry. The client reads SmartHub's interval timestamps as wall clock in Home Assistant's timezone, so a dev instance left on UTC shifts every hour by the co-op's offset:

```sh
# over the WebSocket API, with the agent token
{"type": "config/core/update", "time_zone": "America/New_York", "currency": "USD"}
```

Pointing the Energy dashboard at the entry's statistics is one `energy/save_prefs` call. The grid source schema in 2026.9.1 is flat, and it is strict: `cost_adjustment_day` is required, `name` and `stat_power` are refused when sent as `null`, and everything else takes an explicit `null`.

```json
{"type": "energy/save_prefs", "energy_sources": [{"type": "grid", "stat_energy_from": "nisc_smarthub:<account>_<location>_usage", "stat_energy_to": null, "stat_cost": "nisc_smarthub:<account>_<location>_cost", "entity_energy_price": null, "number_energy_price": null, "stat_compensation": null, "entity_energy_price_export": null, "number_energy_price_export": null, "cost_adjustment_day": 0.0}], "device_consumption": []}
```

Repeated failed logins lock the SmartHub account for about forty minutes. Get the TOTP secret right before driving the config flow, and never script a retry loop around a login.

## Tests

`tests/conftest.py` freezes the clock at 2026-09-11T12:00Z for every test that uses `hass`, inside the billing cycle the recorded fixtures fall in (2026-09-01 through 2026-09-03, cycle day 28). A test that needs another date calls `freezer.move_to`. Writer, reconcile, and finalize tests run on the real recorder through `recorder_mock`; the client is mocked at its boundary from the fixtures in `tests/recordings.py`. Sensor and diagnostics states are Syrupy snapshots; regenerate with `--snapshot-update` only when the shape legitimately changed, and say so in the change.

## Fixtures

`scripts/capture_fixtures.py` takes the portal credentials from its environment; `mise run fixtures:capture` supplies them through `fnox exec` from the `op://agent/...` references in `fnox.toml`. fnox authenticates with the agent-vault service account it finds as a hidden secret in `~/.config/fnox/config.toml`, so nothing prompts and the token never reaches the script. On the laptop that file is enrolled by ansiblonomicon; in an orb, `.agents/resume` writes it from the `OP_SERVICE_ACCOUNT_TOKEN` Amp supplies and drops the variable. Calling `op` directly instead goes through the desktop app, whose per-process authorization prompt is what hangs an unattended run. The script records `user-data`, `billing`, `service-locations/<loc>`, and hourly and daily polls for 2026-09-01 through 2026-09-03, harvests every identifier of every account and location the login can see, replaces them by value everywhere, then keeps only the scalars whose full path is allowlisted and turns every other scalar into a typed placeholder, printing each denied path. `--from-fixtures` reruns the scrub over the recorded fixtures without a capture, which is how a policy change is proved in an orb.

## Orbs

`.agents/setup` installs mise if absent, trusts the config, installs the pinned tools, and syncs the venv; it is idempotent and Amp snapshots its result. `.amp/services.yaml` declares the dev instance on 8123 as a portal. `.agents/resume` installs the agent-vault identity when Amp supplies `OP_SERVICE_ACCOUNT_TOKEN`, then onboards a fresh instance when `config/.agent-token` is absent and the instance answers. `config/configuration.yaml` binds `server_host` to loopback; if a portal cannot reach loopback, change it to `0.0.0.0` (unverified). With the identity in place, fixture capture works in an orb as well as on the laptop.

## Validation that only CI runs

hassfest (`home-assistant/actions/hassfest`) and HACS validation (`hacs/action`) run in `.github/workflows/ci.yml`; the installed Home Assistant wheel does not ship `script/hassfest`, so there is no local equivalent. `mise run check` and `mise run test` are the local gates.

## Home Assistant

The production instance is an appliance reachable only through the `home-assistant` MCP server. The cutover from gagata's integration is a runbook at `docs/runbooks/appliance-cutover.md`.

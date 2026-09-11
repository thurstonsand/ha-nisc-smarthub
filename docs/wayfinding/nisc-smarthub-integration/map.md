# NISC SmartHub integration

## Destination

An accepted design doc in `docs/designs/` with a handoff-safe implementation plan for `nisc_smarthub`: a HACS custom integration that replaces `gagata/ha-smarthub-energy-sensor` on Thurston's Home Assistant with one SmartHub scrape producing usage (total and per period) and cost statistics for the Energy dashboard plus cycle-to-date sensors, built on a tariff interface whose first implementation is Cobb EMC NiteFlex. Building it is a separate effort.

**Reached 2026-09-11**: [`docs/designs/01-nisc-smarthub.md`](../../designs/01-nisc-smarthub.md), Accepted.

## Notes

- Plan only (wayfinder default). Execution tickets are not allowed on this map.
- Skills to consult: `wayfinder` for working the map; `/grill-me` for grilling tickets. Read the repo's `AGENTS.md`, `CONTEXT.md`, and `HA_DEV.md` (once harvested) before any ticket.
- The production Home Assistant is an appliance: HAOS 2026.9.1 in an Incus VM on pod042, reached through the `home-assistant` MCP server. HACS is installed; zero Supervisor add-ons. It currently runs gagata's integration and an Energy dashboard grid source pointed at its hourly usage statistic.
- This repository is public (HACS cannot install private repos). Account, meter, and service-location identifiers and all credentials stay in 1Password ("Cobb EMC (Electrical)", `SmartHub` section), never in the repo.
- Standing decisions from charting (2026-09-11):
  - **Name**: repo `ha-nisc-smarthub`, domain `nisc_smarthub`, statistic prefix `nisc_smarthub:`. Vendor-exact; gagata's domain is `smarthub`, and the two must be able to coexist on one instance during migration.
  - **Replace, don't coexist.** One scraper emits usage and cost from the same response; gagata's is uninstalled at cutover and the grid source repointed. Two scrapers with separate revision windows can disagree about the hour they sit next to.
  - **Fresh implementation.** Port the SmartHub client *logic* (auth with TOTP, poll, parsing) from gagata's with attribution, not the code. Follow Home Assistant Core's style where it has an opinion; otherwise straightforward and maintainable.
  - **Tariff interface now, NiteFlex only.** Periods come from the SmartHub TOU series, never inferred from wall-clock hours. The seam admits a flat-rate tariff later; that implementation is out of scope here.
  - **Rates are user-editable in the config flow**, seeded with published defaults. How rate changes carry an effective date and how a user backfills after missing one is the tariff ticket's question.
  - **Statistics (v1)**: usage total hourly kWh, usage per period hourly kWh, cost total hourly USD. No daily rollups, no per-period cost, no net metering.
  - **Entities (v1)**: cycle-to-date usage kWh, cycle-to-date estimated cost USD, Super Off-Peak allowance remaining kWh, last successful poll / freshness. Sensors align to the billing cycle, not the calendar month. Alerting on the allowance is the user's automation, not ours.
  - **Service charge** is smeared evenly across every hour of the billing cycle.
  - **True-up hook designed now**, bill reader later: the reconcile-window semantics ("oldest unfinalized cycle to now", sum continuity) are part of v1; reading the posted bill for PCA, tax, and round-up waits on the first bill.
  - **Billing cycle start** is a configured day-of-month until SmartHub is shown to expose read dates.
  - **Credentials** live in the config entry, entered through the config flow.
  - **Version floors**: HA >= 2026.9, Python 3.14, local dev HA pinned to what the appliance runs (2026.9.1).
  - **Development loop**: a mise task boots a local Home Assistant with the integration symlinked in; pytest-homeassistant-custom-component for unit tests; the appliance via MCP for final smoke only. Development also happens in Amp orbs (ephemeral Debian 12 VMs), so the loop must work headless from a clean clone.
  - **Agent docs**: `AGENTS.md` / `CONTEXT.md` / `DEV.md` in Thurston's usual shape, plus `HA_DEV.md` harvested from `jpawlowski/hacs.integration_blueprint`'s agent material, kept close to its source's voice.
- Account facts as of 2026-09-11: service connected 2026-08-28, no bills issued yet, billing cycle number 3, service location `taxable`, enrolled in Operation Round Up, meter type `TIME_OF_DAY_KWH_DEMAND`, active rate codes `NFON` / `NFOFF` / `NFSOF`.

## Decisions so far

<!-- one line per closed ticket -->

- [SmartHub API surface](tickets/01-smarthub-api.md): TOTP is mandatory on `oauth/auth/v2`; `user-data` and `billing` are GET, the usage `poll` is an async POST; the hourly poll returns a `TIME_OF_USE` entry that partitions every hour into the utility's own periods. No endpoint returns cost.
- [NiteFlex tariff and bill facts](tickets/02-niteflex-tariff.md): 14.0¢ On-Peak (1p–9p), 7.5¢ Off-Peak, Super Off-Peak (12a–6a) free to 400 kWh/cycle then 5.0¢, $33 service charge; PCA rider, sales tax, and Round Up are bill-time unknowns; first 13 days ran 46/35/19% with ~210 kWh/cycle projected Super Off-Peak, so the allowance is not binding today.
- [Home Assistant cost constraints](tickets/03-ha-cost-constraints.md): prices are rejected on external usage statistics because the cost sensor subscribes to an entity; `stat_cost` pointing at our own USD external statistic is the only hook; `cost_adjustment_day` is dead config; imports upsert by `(statistic_id, start)` and sums must stay continuous across any rewrite.
- [Cobb EMC bill anatomy](tickets/05-bill-anatomy.md): the schedule fixes energy by period, the 400 kWh tier, the $33 service charge, and the 7.75% Fulton sales tax, which applies to service charge + energy + PCA; the bill alone reveals the PCA factor (rider published, value not), short-cycle proration, Operation Round Up (last line, untaxed), one-off fees such as the $30 establishment fee likely on the first bill, and the once-a-year capital credit line that a bill-pinned cost must carry through true-up.
- [Home Assistant statistics import contract](tickets/06-ha-statistics-contract.md): against core 2026.9.1, `async_add_external_statistics` with all seven metadata keys (`energy`/kWh for usage; `unit_class=None` with `unit_of_measurement="USD"` for cost, a deliberate declaration since the import path enforces no currency and `opower` ships no unit at all), aware UTC hour starts, cumulative sums; a rewrite that changes the net offset must replay the stored tail forward; seed strictly before the window from the recorder executor. `opower` is append-after-anchor precedent, not a reconcile algorithm. `_async_validate_cost_stat` checks only that metadata exists.
- [Home Assistant custom integration practice](tickets/07-ha-integration-practice.md): `pytest-homeassistant-custom-component==0.13.364` pins `homeassistant==2026.9.1` (Python >= 3.14.2); dev loop is an ignored `config/` with `config/custom_components -> ../custom_components` and `uv run --no-sync hass --config "$PWD/config" --debug`; long-lived tokens can be minted non-interactively for browserless inspection. Two design facts: a coordinator stops polling with no entity listeners, so statistics writing needs an entry-owned subscription; and Core puts required settings in `data` + reconfigure rather than options.
- [Tariff model and rate versioning](tickets/09-tariff-model.md): a stateless tariff prices a whole cycle, `price_cycle(cycle, hours, versions, actuals=None)`, returning per-hour cost breakdowns; NiteFlex tiers Super Off-Peak chronologically, smears the service charge and a signed recurring adjustment per hour, applies the PCA factor outside the allowance and 7.75% tax on energy + fixed + rider. Rate versions are one dated bundle each, append-only in entry data via a reconfigure menu, effective from local midnight; published versions ship with the tariff and a newer one raises a repair, never auto-applies. Unknown labels write usage, skip cost, raise a repair. Finalized cycles take bill actuals plus a smeared residual so the cycle sum equals the bill. One-off adjustments are out of scope.
- [Statistics writer and reconcile window](tickets/10-statistics-writer.md): every write ends at the newest hour; a routine window covers the cycle containing now-7d to now, and a full pass from the oldest unfinalized cycle runs on first run, any rate or cycle-day change, any true-up, or the `reconcile` action; seeds come from a `[floor, W)` range read and an unexpected gap promotes the run to full; rows are diffed before write and revisions logged; cycle records live in a per-entry `Store`; `finalize_cycle` / `unfinalize_cycle` actions are the true-up hook a bill reader will call later; polling is a coordinator with an entry-owned listener.
- [Config flow, options, and credentials](tickets/11-config-flow.md): three steps (portal, location from `user-data`, tariff) with the tariff preselected from the location's rate schedule codes and rates seeded from the tariff's published version; a verification poll before create; data holds everything but the poll interval; unique id `<account>_<location>`; reconfigure is a menu (add/remove rate version, cycle day, credentials); diagnostics redact by value as well as key; schema may break until the first tag.
- [Entities and device model](tickets/12-entities.md): `cycle_usage` and `cycle_cost` as `total` with `last_reset` at cycle start, one `allowance_remaining` per tiered period, `last_poll` and `newest_data_hour` diagnostics; values from the coordinator result; one device per entry with the meter as serial and the tariff as model.
- [Development loop, tests, CI, and packaging](tickets/13-dev-loop.md): blueprint-shaped tracked `config/configuration.yaml` with `default_config`, `mise run dev` symlinking `custom_components` and running `hass --debug`; HA 2026.9.1 and its test plugin pinned, runtime closure installed by `hass` for now and declared in uv as the first follow-up; headless `dev:bootstrap` mints the agent token; Amp orbs supported through `.agents/setup`, `.amp/services.yaml`, `.agents/resume`; recorded fixtures scrubbed by a deterministic map with a leak check; Core's Ruff set; 95% coverage gate; hassfest and HACS actions; `mise run release`; client bundled.
- [Shared rate-data sources](tickets/15-shared-rate-sources.md): no. The URDB's Cobb EMC entries froze on 2018-05-22 without NiteFlex, and SmartHub's `secured/rates` answers `Allow: OPTIONS` to everything. A second utility is a data file plus a period-label mapping, hand-transcribed. Side find: `GET secured/service-locations/<loc>` returns premise geography and tax-jurisdiction ids.
- [Design doc and implementation plan](tickets/14-design-doc.md): `docs/designs/01-nisc-smarthub.md` accepted with sixteen decisions and an eight-phase plan; the dependency closure and the bill reader remain open alternatives.
- [Harvest HA_DEV.md](tickets/08-harvest-ha-dev.md): `HA_DEV.md` (856 lines, 16 sections) from jpawlowski at `0df1589`, cut to a cloud-polling coordinator integration; four stale claims dropped (`UpdateFailed(retry_after=)`, 120-column Python, unregistered pytest markers, `freezer` misattributed). The source has nothing on external statistics, so the recorder contract comes from ticket 06, not the harvest.
- [Scaffold the repository](tickets/04-scaffold-repo.md): `../ha-nisc-smarthub` exists with mise, hk, renovate, ruff/basedpyright/pytest, markdownlint, CI, MIT, the gitignored `.mcp.json`, and the agent docs skeleton.

## Not yet specified

- **Bill-driven true-up implementation.** Sharpens once the first bill posts (expected early October 2026): whether `/services/secured/billing` or a sibling itemizes charges per bill, whether it exposes the read dates that define the cycle, and what the PCA, tax, and Round Up lines look like. Until then only the hook is designed. The first bill is a short cycle and will also show whether the 400 kWh allowance prorates with the service charge, which the schedule leaves unsaid.
- **Rate-change ergonomics beyond effective dates.** If the tariff ticket lands on dated rate versions, there may be a follow-on about surfacing "your rates may be stale" to the user.

## Out of scope

- Building, releasing, and installing the integration, and the appliance cutover from gagata's (uninstall, repoint the grid source). That is the next effort, seeded by this map's design doc.
- A flat-rate tariff implementation and any second utility. The design leaves the seam.
- Net metering / return-to-grid statistics. gagata's proves the API supports it; not in v1.
- Submission to the HACS default store.
- Allowance alerting. The integration exposes the metric; automations are the user's.
- One-off per-cycle adjustments (establishment fee, capital credit) entered by hand. The true-up's residual absorbs them once bills are read.

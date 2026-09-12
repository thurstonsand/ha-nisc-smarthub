# NISC SmartHub for Home Assistant

A custom integration for electric co-ops that run the NISC SmartHub member portal. It pulls hourly usage, keeps the utility's own time-of-use classification of every hour, prices it under your rate schedule, and writes usage and dollars into Home Assistant's long-term statistics, so the Energy dashboard shows kWh and cost side by side for a utility that publishes no cost of its own.

One config entry tracks one service location. The first rate schedule it models is Cobb EMC's NiteFlex.

## What you get

- Hourly kWh for the service location, from the account's connect date forward, as an external statistic the Energy dashboard reads as a grid source.
- Hourly dollars for the same hours, priced from your rate schedule, as the grid source's cost statistic.
- One kWh statistic per time-of-use period, so a template or a card can show how the day split between on peak, off peak, and super off peak.
- Sensors for the cycle to date, the remaining super off-peak allowance, and how fresh the data is.
- A true-up action that reprices a whole billing cycle from the bill when it posts, so a finalized cycle's cost statistics match the bill to the cent.

## Supported accounts

Any co-op that serves its members through a SmartHub portal at `<coop>.smarthub.coop`, as long as the account has two-factor authentication enabled and you can read the TOTP secret. The portal's JSON API is undocumented; the integration speaks the same endpoints the web app does.

Pricing is another matter. The only tariff this package ships is Cobb EMC's NiteFlex, matched by the rate schedule codes `NFON`, `NFOFF`, and `NFSOF` that SmartHub reports for the location. If your location reports other codes, setup aborts and names the codes it saw. Another rate schedule is a new tariff class plus its numbers, which is a code change, not a setting. The seam it plugs into is described in [`docs/designs/01-nisc-smarthub.md`](docs/designs/01-nisc-smarthub.md) under "Coordinator to tariff".

Accounts with more than one service location work; each location needs its own entry. An account served by more than one provider is refused, because the integration cannot tell which provider's rates to price it under, and so is a service location with more than one meter, because the portal's series are per meter and adding them up is a guess.

## Prerequisites

- A SmartHub login: the portal host, the email address, and the password.
- Two-factor authentication enabled on that login, and its TOTP secret, the setup key the portal shows when you enrol a code generator. Not a six-digit code. The integration generates a fresh code for every login, and the portal wants one every time.
- The day of the month your billing cycle starts.
- The Home Assistant instance's timezone set to the co-op's. SmartHub timestamps its intervals in local wall clock, and the integration reads them in Home Assistant's timezone.

## Installation

The integration is not in the HACS default store. Add it as a custom repository:

1. In Home Assistant, open HACS.
2. From the menu in the top right, choose **Custom repositories**.
3. Paste `https://github.com/thurstonsand/ha-nisc-smarthub`, choose the **Integration** type, and add it.
4. Search HACS for **NISC SmartHub** and download it.
5. Restart Home Assistant.
6. Go to **Settings > Devices & services > Add integration** and pick **NISC SmartHub**.

Pre-releases are only offered by HACS when the repository is set to show them.

## Configuration

Every setting is entered in the config flow. There is no YAML.

### Step 1, the portal

| Field | Meaning |
| --- | --- |
| Portal host | The portal's address, such as `cobbemc.smarthub.coop`. A full URL is accepted and stored as the bare host. |
| Email | The address you sign in to SmartHub with. |
| Password | That account's password. |
| Two-factor secret | The TOTP setup key, not a six-digit code. |

The step logs in for real before it moves on. A rejected login shows "The portal rejected the credentials or the two-factor code" and the form comes back.

### Step 2, the service location

Skipped when the login reaches exactly one location. Otherwise pick the premise this entry tracks, and repeat the flow for the others.

### Step 3, the tariff, the rates, and the cycle

The tariff is preselected from the location's rate schedule codes and can be overridden. The nine rate fields are seeded from the newest rate version this package ships, which is NiteFlex effective 2026-01-01:

| Field | Meaning | Seeded value |
| --- | --- | --- |
| Rates effective from | The day these numbers took effect. Hours before it keep the rates that applied then. It cannot be later than the account's connect date, because the first version has to price the oldest hour. | 2026-01-01 |
| On-peak rate | Dollars per kWh between 1:00 PM and 9:00 PM. | 0.14 |
| Off-peak rate | Dollars per kWh between 6:00 AM and 1:00 PM and between 9:00 PM and midnight. | 0.075 |
| Super off-peak rate | Dollars per kWh between midnight and 6:00 AM, after the allowance is spent. | 0.05 |
| Super off-peak allowance | Free super off-peak kWh per billing cycle. | 400 |
| Service charge | Dollars per billing cycle regardless of usage, spread evenly over the cycle's hours. | 33.0 |
| Power cost adjustment | Dollars per kWh for the PCA rider, signed. The bill is the only place it is published, so leave it at zero until you have one. | 0 |
| Sales tax rate | The fraction of energy, service charge, and rider charged as sales tax. 0.0775 is Fulton County outside Atlanta. | 0.0775 |
| Recurring adjustment | A signed amount per cycle, untaxed, for a standing discount or fee the schedule does not state. | 0 |
| Cycle start day | The day of the month the billing cycle starts. A day past the end of a short month falls on that month's last day. | 1 |

A three-day usage poll has to succeed before the entry is created, so a portal that authenticates but will not report usage fails setup instead of producing an empty entry.

### Options

**Poll interval**, in whole minutes. The default is 360 and the minimum is 30. SmartHub publishes hourly data about a day late, so there is nothing to win below a few hours.

## Statistics

Five external statistics per entry, one row per hour, cumulative sums from the account's connect date. `<account>` is the account number and `<location>` the service location id.

| Statistic id | Unit |
| --- | --- |
| `nisc_smarthub:<account>_<location>_usage` | kWh |
| `nisc_smarthub:<account>_<location>_usage_on_peak` | kWh |
| `nisc_smarthub:<account>_<location>_usage_off_peak` | kWh |
| `nisc_smarthub:<account>_<location>_usage_super_off_peak` | kWh |
| `nisc_smarthub:<account>_<location>_cost` | USD |

To show them on the Energy dashboard, open **Settings > Dashboards > Energy**, add a grid consumption source, and pick the `_usage` statistic. For its cost, pick the `_cost` statistic as the statistic that tracks total costs. Do not set a price per kWh on that source: the Energy dashboard refuses price fields on a statistic that has no entity behind it, which is the reason the cost statistic exists.

Written into the energy preferences directly, the grid source is flat, and `cost_adjustment_day` is required even though nothing reads it:

```json
{
  "type": "grid",
  "stat_energy_from": "nisc_smarthub:<account>_<location>_usage",
  "stat_energy_to": null,
  "stat_cost": "nisc_smarthub:<account>_<location>_cost",
  "entity_energy_price": null,
  "number_energy_price": null,
  "stat_compensation": null,
  "entity_energy_price_export": null,
  "number_energy_price_export": null,
  "cost_adjustment_day": 0.0
}
```

The statistics are the product. They keep being written with every sensor disabled.

## Entities

One device per entry, with these sensors.

| Sensor | What it holds |
| --- | --- |
| Cycle-to-date usage | kWh so far in the current billing cycle. |
| Cycle-to-date cost | Dollars so far in the current cycle. Its attributes carry the breakdown: energy, service charge, rider, sales tax, recurring adjustment, residual, and the count of priced and unpriced hours. |
| Super off-peak allowance remaining | The free super off-peak kWh left in the cycle. |

The three cycle sensors carry no state class, on purpose. The statistics above are the long-term record; a state class would have the recorder build a second, coarser history from sensor states that lag the portal by a day at every cycle boundary.
| Last poll | When the last run finished. Diagnostic. |
| Newest data hour | The newest hour SmartHub has reported. Diagnostic. Roughly a day behind now, which is normal. |

## Actions

### `nisc_smarthub.reconcile`

| Field | Required | Meaning |
| --- | --- | --- |
| `config_entry_id` | yes | The entry to recompute. |
| `from` | no | The first day to recompute. The window opens at the start of that day's billing cycle. Left empty, the whole history is recomputed from the oldest cycle without a bill. A day after today is refused. |

It recomputes and rewrites; it never clears rows.

### `nisc_smarthub.finalize_cycle`

| Field | Required | Meaning |
| --- | --- | --- |
| `config_entry_id` | yes | The entry the bill belongs to. |
| `cycle_start` | yes | The day the cycle this bill covers starts on, as the integration already has it. |
| `read_start` | yes | The bill's first read date. It replaces the calendar start of the cycle. |
| `read_end` | yes | The bill's last read date. The next cycle starts there. |
| `service_charge` | yes | The fixed charge the bill lists. |
| `pca_factor` | yes | Dollars per kWh for the rider the bill reveals, signed. |
| `tax` | yes | The bill's tax line, in dollars. |
| `bill_total` | yes | What the bill charges. |

A true-up replaces that cycle's estimated service charge, rider, tax, and calendar bounds with the bill's, reprices every hour in it, and smears the difference between the bill total and the sum of the priced components across the cycle's priced hours as a residual. The cycle's cost statistics then sum to the bill total exactly. A large residual means the tariff is missing a line on your bill, which is worth reporting. A finalized cycle stops repricing: a rate version dated inside it governs only the cycles after it, and a cycle-day change leaves its read-date bounds alone. The read dates replace the calendar boundaries nearest to them, so a bill read a day or two off the cycle day moves the boundary rather than leaving a sliver cycle beside it. Because a read date can move the boundary of the cycle before it, finalizing triggers a full pass from the hour before the earlier of the cycle's calendar start and its read start.

The action refuses a read end after today, read dates that overlap another recorded cycle, and a cycle whose last hour the portal has not reported yet. Wait for the portal to catch up before finalizing a cycle that just ended.

### `nisc_smarthub.unfinalize_cycle`

| Field | Required | Meaning |
| --- | --- | --- |
| `config_entry_id` | yes | The entry the cycle belongs to. |
| `cycle_start` | yes | The day the finalized cycle starts on. |

Drops the recorded bill and prices the cycle from the rate schedule again.

## Changing an entry later

**Reconfigure** (the entry's menu) offers four things:

- **Add a rate version**: a new dated set of rates. Every hour from its day forward reprices under it; earlier hours keep the rates that applied then, and finalized cycles keep their bills. A date already taken is refused.
- **Remove the newest rate version**: the one before it prices those hours again. The last remaining version cannot be removed.
- **Billing cycle start day**: unfinalized cycles re-split, finalized ones keep their bills' read dates.
- **Portal credentials**: email, password, TOTP secret. The new login has to reach the same service location, or the flow refuses it.

Each of these triggers a full reprice of everything it could have changed.

**Reauth** starts on its own when the portal stops accepting the stored credentials, and asks for the password and the TOTP secret.

## Data updates

The integration polls every 6 hours by default. Each run polls once and rewrites a window of hours rather than appending to the end:

- A routine run recomputes from the start of the billing cycle containing seven days ago through the newest hour SmartHub has.
- A full pass, from the oldest cycle without a bill, runs on the first run after a restart, after any rate or cycle change, after a true-up, and when you ask for one with `reconcile`.
- SmartHub publishes hourly data about a day late, and lags its time-of-use classification behind its usage at the tail. Hours it has not classified yet carry kWh and no cost until the next run that finds them classified; a poll that arrives without any classification leaves the stored cost alone.
- SmartHub revises past hours. A revision is rewritten along with every later cumulative sum, so the dashboard bar changes without a spike. The log carries one line per series per run with the count and range at INFO, and each hour's old and new values at DEBUG.
- An hour that was stored and then vanishes from the portal's usage is rewritten to zero in every statistic rather than left standing, because the recorder cannot delete a row.
- Every portal request times out after 30 seconds and the run retries at the next interval.

## Repairs

| Issue | What it means | What to do |
| --- | --- | --- |
| `unclassified_hours` | SmartHub reported usage for hours it never classified into a rate period, and they sit between hours it did classify. Those hours carry kWh and no cost. | Nothing. The next run that finds them classified prices them and clears the issue. It appears only for interior hours; the unclassified hours at the start of the account's history and at the tail are expected and only logged. |
| `unknown_period_label` | The portal classified hours under a period name this tariff does not price. | Report the labels named in the issue. It needs a release. |
| `newer_rate_version` | A release ships rates newer than any version this entry has. | Compare them with your bill and add the version through Reconfigure. Rates are never applied for you, because repricing your history under a rate you are not billed under would rewrite it wrongly. |

## Known limitations

- Bills are not read. The true-up is an action you run with the numbers off your bill.
- NiteFlex is the only tariff. Flat rates and other co-ops' schedules need a tariff class.
- The first cycle's proration is a guess. It takes the full service charge and the full allowance regardless of its length, because how Cobb EMC prorates a short first cycle is not published. The first bill's true-up corrects it.
- The fall-back hour of a daylight-saving transition is read as two instants at the same wall clock. How the portal actually encodes that night is unverified until 2026-11-01.
- HACS custom repository only. This is not in the HACS default store and not in home-assistant/brands, so it has no brand icon in the UI.
- Return to grid, net metering, and compensation statistics do not exist.

## Troubleshooting

**Never loop a failed login.** SmartHub locks an account for about forty minutes after a few failed attempts. If reauth fails, fix the credentials before trying again rather than retrying the form.

**A wrong password looks like success from the outside.** The portal answers HTTP 200 with a failure body and no token. The client reads that as a rejection and starts reauth.

**Tokens last 299 seconds**, so every run logs in again. A login per poll is expected, not a bug.

**No dollars, only kWh.** Check the cycle-to-date cost sensor's `unpriced_hours` attribute and the repairs page. Hours with no time-of-use classification are written as usage with no cost.

**Numbers do not match the bill.** Until you run `finalize_cycle`, the cost is an estimate that does not know your power cost adjustment. Its rider component is zero unless you entered a PCA factor.

**Diagnostics.** Download them from the entry's menu. The dump redacts the password, the TOTP secret, and the email by key, and the account number, the service location id, and the meter number wherever they appear, statistic ids included.

## Removal

Delete the entry from **Settings > Devices & services**, then remove the repository from HACS if you are done with it.

Deleting the entry removes its device, its sensors, and its stored cycle records at `.storage/nisc_smarthub.<entry_id>`. It does not remove the statistics, because the recorder keeps long-term statistics independently of whatever wrote them, and reinstalling reuses the same ids and repairs the history rather than orphaning it. To clear them for good, use **Developer tools > Statistics** and delete the five ids, or send `recorder/clear_statistics` with them over the WebSocket API.

## Acknowledgements

The SmartHub client logic descends from [gagata/ha-smarthub-energy-sensor](https://github.com/gagata/ha-smarthub-energy-sensor) (MIT), which proved the portal's authentication and usage-poll endpoints and the shape of the requests they want. `custom_components/nisc_smarthub/smarthub/client.py` is a reimplementation of that knowledge rather than a fork, and adds the time-of-use series and the cost statistics that integration discards.

## Development

See [`DEV.md`](DEV.md) for the local Home Assistant instance, the test suite, and the release task, and [`docs/designs/01-nisc-smarthub.md`](docs/designs/01-nisc-smarthub.md) for why the thing is shaped the way it is.

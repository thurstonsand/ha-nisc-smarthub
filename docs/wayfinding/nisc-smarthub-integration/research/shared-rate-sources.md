# Shared rate-data sources

Whether any machine-readable source carries a utility's current rate schedule well enough to seed a tariff. Probed 2026-09-11: the OpenEI Utility Rate Database (API and bulk dump), `cobbemc.smarthub.coop`'s `secured/rates` and `secured/service-locations` with the account's credentials, and Cobb EMC's own rate pages. Identifiers are redacted as `<acct>`, `<loc>`, `<meter>`.

## OpenEI URDB

### API contract

`GET https://api.openei.org/utility_rates` with `version` (3 through 7), `format`, and `api_key` required; a call without a key returns `{"error": {"code": "API_KEY_MISSING"}}`. `DEMO_KEY` works and is enough to answer this question, but it is metered at ten requests: the eleventh came back **HTTP 429** with `x-ratelimit-limit: 10, x-ratelimit-remaining: 0`. Registration is free. Useful selectors are `ratesforutility` (exact utility page label), `eia`, `address` with `radius`, `sector`, `effective_on_date`, `modified_after`, and `getpage` for a single rate id; `limit` caps at 500 ([API docs, version 7](https://openei.org/services/doc/rest/util_rates/?version=7)).

The bulk dump sidesteps the key entirely. `https://openei.org/apps/USURDB/download/usurdb.csv.gz` is 12.2 MB gzipped and its `Last-Modified` was 2026-09-10, one day before this probe, so the export pipeline is alive even where the rows are not. Everything below about the whole database comes from that dump.

### Cobb EMC's entries

The utility page is `Cobb Electric Membership Corp`, EIA id 3916, with fifteen rates. Eight carry `startdate` 2014-01-01 and seven 2018-01-01. Every one of them has a `latest_update` of 2018-05-22, except two Critical Peak Pricing entries last touched 2015-03-26. The database's default residential rate for Cobb is "Residential Service R-14"; the others are "Residential Service R-12: Rate 10", "Residentail Service R-12-TOU: Rate 12T" (the typo is theirs), and "Residential Optional Rate Schedule RES-01: Simple Bill". None of these schedule names survive on Cobb's site today, and the `source` links point at PDF paths under `cobbemc.com/sites/cobbemc/files/Current Site PDFs/` that the site no longer uses.

**NiteFlex is not there.** It is not there under any spelling, and neither is Standard, Fixed, Smart Choice, or Even Bill. The URDB's picture of Cobb EMC is eight years old and describes a rate lineup the co-op has since replaced wholesale.

Grepping the entire US dump for "niteflex" returns three rows, all of them **Middle Tennessee E M C** (EIA id 12470), an unrelated co-op that sells a time-of-use residential schedule under the same trade name. Those entries are maintained the way the URDB is supposed to work: a 2024-07-01 version ending 2025-03-31, a 2025-04-01 version ending 2026-03-31, and a current 2026-04-01 version with `latest_update` 2026-04-27. Two utilities using the same marketing name for different tariffs is its own small hazard for any code that matches on rate names.

### How the encoding handles a NiteFlex shape

The schema is not the problem. Middle Tennessee's current entry looks like this, trimmed:

```text
fixedchargefirstmeter = 21.81            fixedchargeunits = $/month
energyratestructure/period0/tier0rate = 0.05108   adj = 0.03717   unit = kWh
energyratestructure/period1/tier0rate = 0.09108   adj = 0.03717   unit = kWh
energyweekdayschedule = [[0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,0,0], ...12 rows]
energyweekendschedule = [[0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,0,0], ...12 rows]
energycomments = TVA Fuel Cost Adjustment
```

Periods are a list of lists: outer index is the period, inner index is the tier, each tier carrying `rate`, optional `max` with `unit`, and `adj`. The schedules are two twelve by twenty-four matrices of period indices, one for weekdays and one for weekends, month by month and hour by hour from midnight. Everything Cobb's NiteFlex needs maps onto that: three periods instead of two, the weekend matrix filled identically to the weekday matrix because NiteFlex ignores the distinction, `fixedchargefirstmeter = 33`, and the Super Off-Peak allowance as `tier0` with `max: 400, unit: kWh, rate: 0` followed by `tier1` at `0.05`. The zero-rate tier is not a theoretical capability: 1,817 rows in the dump already use one, mostly for the "first N kWh included in the service charge" pattern, such as City of Attica, Kansas billing its first 50 kWh at zero.

Two things the format cannot carry faithfully. The PCA lives in `adj`, a per-tier constant frozen at transcription time, so it is a guess about a number that is only knowable when the bill posts; the exclusion of the free 400 kWh from the PCA is expressible by leaving `adj` off `tier0`, which is at least honest. And a tier's `max` is per billing period by convention with no field saying so, so the URDB cannot distinguish a calendar-month allowance from one that resets on the meter read date. Neither of these removes the need for bill-time true-up.

### Staleness for co-ops

The URDB wiki states that rates are refreshed annually for roughly 150 utilities covering 70% of US load. A suburban Atlanta co-op is not on that list, and the sample bears it out. Among Georgia neighbors: Sawnee EMC has 23 rates with a newest `startdate` of 2025-01-02 and a revision on 2025-01-15, genuinely current. Walton EMC has 11, frozen at 2018 exactly like Cobb. Jackson EMC and GreyStone Power return nothing under those page labels at all. Whether a given co-op is usable is a coin flip, and the coin has already landed wrong for this one.

**Verdict**: the URDB returns nothing at all for NiteFlex, and what it returns for Cobb EMC would price the member's hours against schedules that stopped existing years ago. No tariff can be seeded from it here.

## SmartHub `secured/rates` and `secured/service-locations`

Authenticated per [smarthub-api.md](smarthub-api.md): form POST to `oauth/auth/v2` with `userId`, `password`, and a live TOTP code, then `Authorization: Bearer <token>` plus `X-Nisc-Smarthub-Username`. Auth returned 200 `SUCCESS`. Then, against both collection paths, POST with five bodies: `{}`, the poll-shaped `{userId, accountNumber, serviceLocationNumber, industries: ["ELECTRIC"]}`, `{userId}` alone, `{userId, accountNumber}`, and the poll shape plus `serviceType: "ELEC"` and `providerId: "COBB"`. All ten returned **405** with an empty body, and PUT on each returned 405 as well.

The answer was in a header rather than a body. Every 405 carries `Allow: OPTIONS`, and OPTIONS itself returns 200 with no content and no `Access-Control-Allow-Methods`, even with an `Origin` and `Access-Control-Request-Method: POST` preflight. HEAD, GET, DELETE, and PATCH all join the 405 pile. These two collection paths have no data method registered at all; only the CORS preflight handler answers them. The earlier reading that a 405 means "exists, wants another method" was half right. They exist. They want nothing.

Child paths tell a different story. `secured/rates/<anything>` is a Drupal-style HTML 404, so nothing hangs below it. `secured/service-locations/<anything>` returns 500 for junk segments and **200 for the real id**:

```text
GET /services/secured/service-locations/<loc>     200
```

with Bearer and username headers, no query parameters needed. The body is a flat object about the premise, with no pricing anywhere in it:

```json
{"id": "<loc>", "zipCode": "...", "serviceDescription": "Electric Residential Service",
 "address": "...", "city": "...", "county": "...", "state": "...", "latitude": "...", "longitude": "...",
 "revenueAreaId": "...", "taxDistrictId": "...", "franchiseTaxId": "...", "otherTaxJurisdictionId": "...",
 "schoolDistrictId": "...", "censusBlockNumber": "...", "locationType": "...", "serviceArea": "...", "uuid": "..."}
```

`createProrations` is false and `speedTestLocation` is false; the rest is geography and tax jurisdiction bookkeeping. The three tax id fields are worth passing to the bill-anatomy ticket, since they are the utility's own statement of which jurisdictions apply to this premise, but they are ids into NISC's tables and carry no rates.

Speculative read-only names went nowhere: `secured/rate-schedules`, `secured/rate-schedule`, `secured/tariffs`, `secured/rate-comparison`, `secured/rate-analysis`, `secured/utility-rates`, and `secured/rates/schedules` are all HTML 404. Nothing that suggests mutation was called.

**Verdict**: SmartHub tells you *which* schedule a member is on and never what it costs. `user-data` gives `activeRateSchedules = ['NFON:COBB', 'NFOFF:COBB', 'NFSOF:COBB']`, `billing` gives `primaryRateScheduleId = "NFON"` and the meter's `rateSchedule`, and that is the end of it. Codes and identifiers, no prices. Which is consistent with the rest of the API: the portal computes bills server-side and only ever ships the result.

## Cobb EMC's own site

Confirmed negative. `/rates` links seven 2026 PDFs (NiteFlex, Standard, Fixed, Smart Choice, Even, PCA, and a February DG schedule) plus a printable rate table PDF, and carries no rate data of its own; the only JSON on the page belongs to New Relic and Drupal's asset loader. The per-schedule calculators at `/niteflex-rate-calculator`, `/standard-rate-calculator`, and friends are Drupal webforms that POST back to their own URL and render the answer server-side. No JSON, no CSV, no client-side rate table to scrape, no API behind the rate selection tool. Transcription from the PDF is the only path, which is what the tariff research already did.

## Would a second utility be data or code?

Data, mostly, and not because a shared source will hand it to us.

Nothing about NiteFlex is structurally special. Three named periods, one of them tiered with a per-cycle allowance, a fixed service charge, a per-kWh rider unknown until the bill, a tax, and a round-up. A second co-op on a two-period TOU schedule with no allowance is a strictly simpler instance of that model, and it should be a new dated rate version in a data file, not a new tariff class. Code is needed only when a schedule has a shape the model does not have: seasonal rates that vary by month, demand charges, a net-metering credit, a minimum bill that actually binds.

The real coupling is not the rates. It is the period names. The `TIME_OF_USE` series arrive with `seriesId` values like `"<meter> - Super Off Pk"`, and that string is the utility's own label passed through NISC, not a NISC constant. Every new utility therefore needs its period labels mapped to the tariff's periods, and that mapping is data too, as long as the design keeps it in the tariff definition rather than in a match statement. Worth stating as a design constraint now: a tariff is a list of periods, each with a portal label, a rate, and optional tiers, and the pricing code never mentions "On-Peak" by name.

What no source will do is fill that data in for us. The URDB is the only candidate with a real schema, and for this utility it holds nothing. For a co-op it does cover, and covers recently, an optional URDB import could plausibly pre-fill a tariff's periods, tiers, and service charge from a `getpage` fetch, subject to a free API key and rate limits that are fine for a one-time config-flow call. It would have to be offered as a suggestion the member confirms against their own PDF, never as a silent default: the API will happily serve a 2018 rate with no warning louder than a `latest_update` field. That is an optional convenience for a later effort, not a foundation. The design's hard-coded dated versions with user overrides stand.

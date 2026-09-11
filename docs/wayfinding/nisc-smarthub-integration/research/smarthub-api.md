# SmartHub API surface

Findings from exercising `cobbemc.smarthub.coop` directly on 2026-09-10 and 2026-09-11 with the account's real credentials, cross-read against [gagata/ha-smarthub-energy-sensor](https://github.com/gagata/ha-smarthub-energy-sensor) `custom_components/smarthub/api.py` (v2.2.0) and [tedpearson/electric-usage-downloader](https://github.com/tedpearson/electric-usage-downloader). NISC publishes no API documentation; everything below is observed behavior. Identifiers (account, service location, meter, address) are in 1Password and deliberately absent here.

## Base and headers

All endpoints are under `https://<coop>.smarthub.coop/services/`. Authenticated calls send:

```text
Authorization: Bearer <authorizationToken>
X-Nisc-Smarthub-Username: <login email>
Content-Type: application/json
```

gagata's client also sends `Authority: <host>` and a fixed `User-Agent`; neither was required in testing.

## Authentication: `POST oauth/auth/v2`

Form-encoded (`application/x-www-form-urlencoded`): `userId`, `password`, and `twoFactorCode`.

- With MFA enabled on the account, omitting `twoFactorCode` returns **HTTP 500** with the body `Your data could not be verified. If the problem persists, please contact customer service.` It is not a 401. A client that treats 500 as "connection error" (gagata's does) will retry a permanent failure.
- With a valid TOTP code the response is 200 JSON: `status`, `authorizationToken`, `username`, `primaryUsername`, `expiration`, `expiresIn`, `isSecondaryRegistration`, `isBusinessUser`.
- The integration therefore stores the **TOTP secret** and mints a code per login (gagata uses `pyotp.TOTP(secret).now()`), not a one-time code.
- Token lifetime was not measured; gagata refreshes on 401 and re-authenticates when its session is older than `SESSION_TIMEOUT`.

## Account facts: `GET secured/user-data?userId=<email>`

Returns a list, one entry per customer, each with `account`, `customerName`, `inactive`, `primaryServiceLocationId`, `services` (e.g. `["ELEC"]`), and `serviceLocationToUserDataServiceLocationSummaries`: a map from service-location id to a list of summaries with `description`, `address`, `serviceStatus`, `services`, `activeRateSchedules`, `lastBillPresReadDtTm`, `lastBillPrevReadDtTm`.

Observed for this account: one location, `ACTIVE`, `activeRateSchedules = ['NFON:COBB', 'NFOFF:COBB', 'NFSOF:COBB']` (the three NiteFlex period rate codes), and both bill read dates `null` because no bill has been issued.

`POST` to this path returns 405; it is GET-only with query parameters.

## Billing facts: `GET secured/billing?userId=<email>&accountNumber=<acct>`

Returns a list with `customerSummary` (name, address, contact, hint question **and hint answer in plaintext**, so never log this response) and `accountBillingMap[<acct>]` with:

- `billingCycle` (an integer cycle number, **3** here; not a day of month), `numberOfBills` (**0**), `hasUnbilledUsage`.
- `serviceBillingMap.ELEC.providerBillingMap.COBB` with `billingSummary` (balances, aging), `connectDate` (epoch ms; **2026-08-28** here), `revenueClass = RESIDENTIAL`, `primaryRateSchedule = "NiteFlex On Peak"`, `primaryRateScheduleId = "NFON"`, `roundUpSummary` (Operation Round Up; `status: true` = enrolled), `roundUpSettings`, and `serviceLocationMap[<loc>]` with `taxable` (**true**), `tvaCode`, `meters[<meter>]` (`rateSchedule`, `meterType = "TIME_OF_DAY_KWH_DEMAND"`, `status`).

Paths that do **not** exist or are not GET: `billing-history`, `bills`, `billing/history` (403), `statement-history`, `payment-history`, `bill-details`, `ebill`, `utility-rates`, `usage-summary`. `secured/rates` and `secured/service-locations` answer **405** to GET, so they exist and want another method; not explored. With zero bills issued nothing about per-bill itemization could be verified; that is the map's fog.

## Usage: `POST secured/utility-usage/poll`

JSON body, as gagata sends it:

```json
{
  "timeFrame": "HOURLY",
  "userId": "<email>",
  "screen": "USAGE_EXPLORER",
  "includeDemand": false,
  "serviceLocationNumber": "<loc>",
  "accountNumber": "<acct>",
  "industries": ["ELECTRIC"],
  "startDateTime": "<epoch ms as string>",
  "endDateTime": "<epoch ms as string>"
}
```

`timeFrame` accepts `HOURLY`, `DAILY`, `MONTHLY`. The call is **asynchronous**: the first response is `{"status": "PENDING"}`; repeating the identical request a few seconds later returns `{"status": "COMPLETE", "data": {...}}`. One retry after ~4 s sufficed in every test. `screen` values `BILLING`, `COST`, `MY_USAGE` return `status: FAILED`. Extra keys (`includeCost`, `dataTypes`) are ignored.

`data.ELECTRIC` is a list of entries. Each has `type`, `unitOfMeasure` (`KWH`), `connectDate`, `hasDaily`, `hasHourly`, `series`, `meters`, `estimatedSeries`, `editedSeries`, `baseSeries`, `meterToChartData`. Two entry types were observed:

- **`USAGE`**: `meters` has one entry per meter with `seriesId` = meter number, `flowDirection` (`FORWARD` here; gagata handles `NET` and `RETURN` for solar), `unitOfMeasure`. `series` has one item named by `seriesId` with `data: [{x, y, enableDrilldown}]`, `x` in epoch milliseconds, `y` in kWh.
- **`TIME_OF_USE`**: `meters` has one entry **per period** with `seriesId` like `"<meter> - Off Peak"`, `"<meter> - On Peak"`, `"<meter> - Super Off Pk"`, and `series` likewise. In a 3-day hourly pull the three series had 21 + 16 + 12 = 49 points and `USAGE` had 49: **every hour appears in exactly one period series**, and its `y` equals the `USAGE` value for that hour. The same entry appears at `DAILY` granularity with per-day period totals.

gagata's parser keeps only `type == "USAGE"` and logs the rest as "Unknown Usage", so the classification is discarded upstream today.

Timestamps: `x` values are epoch ms. gagata interprets them as "provider local time expressed as if UTC" and re-attaches the configured timezone (`parse_epoch_set_timezone`); the hourly values it stored in HA line up with local hours after that treatment. The design should verify this against one known hour rather than inherit the assumption.

No entry type carries cost. Cost exists only on issued bills.

## Revision behavior (from gagata's `sensor.py`, read by a subagent)

gagata re-imports from `last_stat - 2 days` on every poll and skips rows at or before its seed, so its effective revision window is about two days, not the 30-day request span, and any hour it misses is never backfilled. SmartHub can revise a past hour; how far back it does so was not measured.

## What this means for the design

- Authentication needs the TOTP secret and must treat the 500-with-message case as an auth failure.
- One hourly poll gives usage and the utility's own period classification. No wall-clock period logic is needed and DST is not our problem.
- `user-data` can populate the account and service-location select in the config flow; `billing` gives connect date, cycle number, taxability, and Round Up enrollment.
- Per-bill detail (PCA, read dates) is unverified until a bill exists.

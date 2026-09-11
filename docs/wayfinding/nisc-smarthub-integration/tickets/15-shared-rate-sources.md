---
status: closed
type: research
claimed: subagent
blocked-by: []
---

# Shared rate-data sources

## Question

Whether any machine-readable source carries a utility's current rate schedule in a form this integration could seed tariffs from, so a second utility is data rather than a new tariff class. Two candidates and one negative to confirm:

- **NREL's Utility Rate Database (OpenEI URDB)**: does it carry Cobb EMC's residential schedules, is NiteFlex 2026 present with its periods, tiers, and fixed charge, how are TOU periods encoded (weekday/weekend hour matrices), what is the API contract (key, endpoints, rate limits, terms of use), and how stale are entries for co-ops in practice.
- **SmartHub's own `secured/rates` and `secured/service-locations`**: both answered 405 to GET on 2026-09-11, so they exist and want another method. Probe them with the account's credentials using the auth recipe in [research/smarthub-api.md](../research/smarthub-api.md) (POST with a JSON body mirroring the usage poll's `userId`, `accountNumber`, `serviceLocationNumber` is the first guess). Record the request that works and the response shape, or the errors, with identifiers redacted.
- Anything else primary: Cobb EMC publishes PDFs only, as far as the tariff research found; confirm no JSON or CSV behind the rate selection tool.

Output: for each source, what it returns for NiteFlex, whether a tariff could be seeded from it without hand transcription, and the honest answer to "would a second utility be data or code". The design's default stays hard-coded published versions with user overrides; this ticket decides whether a later effort can do better.

## Resolution

Neither candidate can seed a tariff: the URDB's Cobb EMC entries stopped being updated on 2018-05-22 and contain no NiteFlex, and SmartHub's `secured/rates` and `secured/service-locations` answer `Allow: OPTIONS` to every method, so they are CORS handlers rather than data endpoints. See [research/shared-rate-sources.md](../research/shared-rate-sources.md) for the full probe log, the URDB encoding that could express NiteFlex if anyone had entered it, and the verdict that a second utility is data plus a portal period-label mapping, with no source to fill that data in automatically.

# ha-nisc-smarthub

A Home Assistant custom integration for electric co-ops that run the NISC SmartHub member portal. It pulls hourly usage, keeps the utility's own time-of-use classification of each hour, prices it under your rate schedule, and writes both usage and cost into Home Assistant's long-term statistics so the Energy dashboard shows kWh and dollars side by side.

**Status: planning.** The design is being charted under [`docs/wayfinding/nisc-smarthub-integration/`](docs/wayfinding/nisc-smarthub-integration/map.md). No integration code exists yet.

The first rate schedule targeted is Cobb EMC's NiteFlex. The tariff model is a seam, so flat and other time-of-use rates can follow.

## Lineage

The SmartHub client logic descends from [gagata/ha-smarthub-energy-sensor](https://github.com/gagata/ha-smarthub-energy-sensor) (MIT), which proved the portal's authentication and usage-poll endpoints. This project reimplements rather than forks it, and adds the time-of-use series and cost statistics that integration discards.

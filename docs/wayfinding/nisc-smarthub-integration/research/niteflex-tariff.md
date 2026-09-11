# NiteFlex tariff and bill facts

Sources: Cobb EMC's [NiteFlex rate schedule PDF, effective 2026-01-01](https://www.cobbemc.com/sites/default/files/documents/rates/residential/2026/0126_NiteFlex_Rate_Schedule.pdf), the [Rate Selection Tool](https://www.cobbemc.com/rates), the [NiteFlex page](https://www.cobbemc.com/niteflex), [Working for You](https://www.cobbemc.com/working-you) (PCA), and the account's `billing` response (see [smarthub-api.md](smarthub-api.md)). Read 2026-09-11.

## Published rate schedule

Residential Service Schedule "NiteFlex", effective January 1, 2026. Single-phase family dwellings, individually metered. Twelve-month commitment.

| Component | Definition | Rate |
| --- | --- | --- |
| Service charge | per month | $33.00 |
| On-Peak | 1:00 PM – 9:00 PM | 14.0¢ / kWh |
| Off-Peak | 6:00 AM – 1:00 PM and 9:00 PM – 12:00 AM | 7.5¢ / kWh |
| Super Off-Peak, first 400 kWh per month | 12:00 AM – 6:00 AM | 0.0¢ / kWh |
| Super Off-Peak, after 400 kWh per month | 12:00 AM – 6:00 AM | 5.0¢ / kWh |

Periods apply on weekends and holidays the same as weekdays (NiteFlex FAQ). Billing is on hourly AMI reads; a missed read is estimated from history for that hour.

Other schedule terms:

- **Minimum charge**: $33.00 plus Power Cost Adjustment.
- **Power Cost Adjustment (PCA)**: "The above rates shall be increased or decreased subject to the provisions of the Corporation's Power Cost Adjustment Schedule, PCA. Super Off-Peak kWh under 400 kWh per billing period are excluded from the Power Cost Adjustment." Cobb EMC describes the PCA as zero, a charge, or a credit, and says it has returned about $86M to members since 2013. The current value is not published on the site; it appears as a bill line item.
- **Billing period**: monthly meter readings at intervals of approximately thirty days; charges "may be prorated to reflect a thirty-day billing period." Winter/summer proration language exists but NiteFlex has no seasonal rates.
- **Tax**: "The member shall pay any sales, use, franchise or other tax now or hereafter applicable." The rate tool's sample bills exclude taxes and fees.

Sample bill amounts from the rate tool (35% on-peak, 35% off-peak, 30% super off-peak): 500 kWh $70.63, 1000 kWh $108.25, 1500 kWh $148.38.

## Bill-time facts for this account

From the `billing` endpoint on 2026-09-11:

- Service connected **2026-08-28**; **no bills issued**. The first bill will be the first observation of the PCA value, the read dates, and the tax line.
- `billingCycle = 3` (a cycle number, not a day of month).
- Service location `taxable = true`: sales tax is expected on the bill. The tax base and the Roswell/Fulton rate are the bill-anatomy ticket's question.
- **Operation Round Up enrolled** (`roundUpSummary.status = true`): the bill total is rounded up to the next dollar; contributions to date $0.
- Meter type `TIME_OF_DAY_KWH_DEMAND`; rate codes `NFON`, `NFOFF`, `NFSOF`.

## Observed usage, 2026-08-28 through 2026-09-09

From the hourly usage statistic gagata's integration wrote (312 hours, 13 days), bucketed with the same wall-clock boundaries the schedule states (the later `TIME_OF_USE` pull agreed with this bucketing on the days compared):

| Period | kWh | Share | Cost at published rates |
| --- | --- | --- | --- |
| Super Off-Peak | 109.4 | 19% | $0.00 (109 of 400 free) |
| Off-Peak | 207.3 | 35% | $15.55 |
| On-Peak | 267.4 | 46% | $37.44 |
| **Total** | **584.1** | | **$52.99** energy + $33.00 service = **$85.99** |

Blended energy rate $0.0907/kWh. Super Off-Peak averages about 7 kWh/day, projecting to roughly **210 kWh per 30-day cycle**, so the 400 kWh allowance is not reached and the cycle boundary currently changes the cost by zero dollars. On-Peak carries 46% of consumption at the highest rate; every kWh shifted from On-Peak to unused Super Off-Peak is worth 14¢.

## What this means for the design

- The tariff needs: three periods with rates; one tiered period with a per-cycle allowance; a fixed per-cycle service charge; a per-kWh rider unknown until bill time and exempt on the allowance; a tax on some base; a round-up on the total. The first three are exact from the schedule; the rest are estimates until true-up.
- Rates carry an effective date (2026-01-01) and Cobb EMC revises annually, so the model must hold dated versions or accept that past cycles reprice under current rates.
- The allowance is not binding today, which argues for shipping the cycle-day config and the allowance sensor without over-investing in cycle-boundary precision until a bill reveals the read dates.

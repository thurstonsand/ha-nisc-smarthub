# Context

Vocabulary for the integration.

## Language

### The portal

**SmartHub**:
NISC's member portal product, hosted per co-op at `<coop>.smarthub.coop`, with an undocumented JSON API behind the web app.

**Service location**:
A metered premise under an account, identified by a numeric id; the unit the usage poll is scoped to.

**Meter**:
The physical register at a service location, identified by its meter number; the series id in poll responses.

**Usage series**:
The `USAGE` entry of a poll response: one kWh value per interval for the meter.

**TOU series**:
The `TIME_OF_USE` entry of a poll response: the same intervals split into one series per period, as classified by the utility. Every hour lands in exactly one period.

### The tariff

**Rate schedule**:
The utility's published pricing document for a class of service, with an effective date. SmartHub identifies one by **rate schedule codes** qualified by the provider that issued them (`NFON:COBB`, `NFOFF:COBB`, `NFSOF:COBB`). A qualifier is not unique across co-ops, so a tariff is matched to an account by its portal host and its qualified codes together.

**Tariff**:
This integration's executable model of one rate schedule: what it prices from, its periods, allowances, rate parameters and bill lines, and the code that prices a cycle under them. Shared code knows a tariff only by those declarations.

**Period**:
A named time-of-use bucket within a tariff with its own rate. NiteFlex has On-Peak, Off-Peak, Super Off-Peak.

**Tier**:
A quantity threshold within a period after which the rate changes. NiteFlex's Super Off-Peak is free up to the allowance, then 5¢.

**Service charge**:
The fixed monthly amount on the bill regardless of usage. NiteFlex: $33.

**Rate parameter**:
One number a tariff's rate versions carry, declared by the tariff with whether it may go negative. NiteFlex's are its three period rates, allowance, service charge, rider factor, tax rate, and recurring adjustment.

**Rate version**:
One dated set of a tariff's rate parameters with an `effective_from` calendar day. Which hours a version governs is the tariff's rule: NiteFlex prices each hour under the version in effect on its local day; a schedule billed on a bills-rendered basis prices a whole cycle under the version in effect when its bill is rendered.

**Bill line**:
One number a posted bill supplies that the tariff declares it reads, replacing its estimate for that cycle. NiteFlex reads the service charge, the PCA factor, and the tax. Read dates and the bill total belong to every bill.

**Rider**:
A per-kWh adjustment applied on top of the rate schedule and known only when the bill posts. Cobb EMC's is the Power Cost Adjustment (PCA).

### Home Assistant

**External statistic**:
A long-term statistic written by an integration under a `<domain>:<name>` id rather than derived from an entity; the only kind the recorder lets an integration backfill.

**Usage statistic**:
An external statistic in kWh; one for total usage and one per period.

**Cost statistic**:
An external statistic in USD that the Energy dashboard reads through `stat_cost`.

**History floor**:
The earliest hour the integration will ever write: the account's connect date as SmartHub reports it. Cumulative sums start at zero here.

**Reconcile window**:
The span of hours one run recomputes, diffs, and rewrites. It always ends at the newest hour SmartHub has. A **routine** window starts at the beginning of the cycle containing seven days ago; a **full** window starts at the oldest unfinalized cycle or the history floor, and runs whenever a past hour's price could have changed.

**Finalized cycle**:
A billing cycle whose bill has posted, so its rider and true-up values are known and its cost statistics can be frozen.

**True-up**:
Repricing a finalized cycle with the values from its bill and rewriting its cost statistics.

**Cycle record**:
The stored per-cycle facts the integration did not derive from the schedule: bill-observed actuals (service charge, rider factor, tax, read dates, bill total) and the residual. Exists only for finalized cycles.

**Residual**:
The difference between a finalized cycle's bill total and the sum of its priced components, smeared across the cycle's hours so the statistics match the bill to the cent. A large residual means the tariff is missing a line.

**Recurring adjustment**:
A signed per-cycle amount on a rate version, smeared like the service charge and outside the tax base. The escape hatch for a standing discount or fee the schedule does not model.

## Relationships

- A **Service location** has one or more **Meters**; each poll returns one **Usage series** and one **TOU series** per meter.
- A **Tariff** models one **Rate schedule** and declares its **Periods**, **Rate parameters**, and **Bill lines**; a **Rate version** holds its rate parameters for a span of time, and a **Cycle record** holds a bill's lines for one cycle.
- Every hour of the **TOU series** belongs to exactly one **Period**.
- A **Reconcile window** spans one or more billing cycles; a **True-up** turns the oldest into a **Finalized cycle** with a **Cycle record** and shortens the window.

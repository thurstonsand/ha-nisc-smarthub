"""Posted bills from the portal, read as the actuals a true-up prices from.

A bill can cover several service locations and services, so only the lines and
reads of this entry's electric location count. Its read dates are midnight
instants in the portal's zone and are kept as calendar days, like the store.
"""

from datetime import datetime, tzinfo
from typing import Final

from .cycles import local_midnight
from .smarthub import Bill
from .tariff import CycleActuals, Tariff

ELECTRIC: Final = "ELECTRIC"

# Account ledger entries the portal lists beside a bill's charges: the payment
# against the previous bill and the balance it carried. Neither is this bill.
LEDGER_LINES: Final = frozenset({"PAYMENT", "TRANSACTION_INQUIRY_FOR_A_BILLING"})


def posted_actuals(
    bill: Bill, *, location: str, tariff: Tariff, zone: tzinfo
) -> CycleActuals | None:
    """Return what a bill says about one location's cycle, or `None`.

    A bill with no reads for the location did not bill it.
    """
    reads = [
        reading for reading in bill.readings if reading.service_location == location
    ]
    if not reads:
        return None
    charges = [
        charge
        for charge in bill.charges
        if charge.service_location == location
        and charge.industry == ELECTRIC
        and charge.charge_type not in LEDGER_LINES
    ]
    return CycleActuals(
        read_start=_midnight(min(one.previous_read_date for one in reads), zone),
        read_end=_midnight(max(one.present_read_date for one in reads), zone),
        bill_total=round(sum(charge.amount for charge in charges), 2),
        values=tariff.bill_values(tariff.read_bill(charges)),
    )


def _midnight(instant: datetime, zone: tzinfo) -> datetime:
    return local_midnight(instant.astimezone(zone).date(), zone)

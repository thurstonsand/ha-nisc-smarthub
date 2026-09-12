"""Constants for the NISC SmartHub integration."""

from typing import Final

DOMAIN: Final = "nisc_smarthub"

CONF_TOTP_SECRET: Final = "totp_secret"
CONF_ACCOUNT: Final = "account"
CONF_CYCLE_DAY: Final = "cycle_day"
CONF_POLL_INTERVAL_MINUTES: Final = "poll_interval_minutes"
CONF_TARIFF: Final = "tariff"
CONF_RATE_VERSIONS: Final = "rate_versions"

DEFAULT_POLL_INTERVAL_MINUTES: Final = 360
# The portal publishes hourly data about a day late, so a tighter poll only
# hammers it for nothing.
MINIMUM_POLL_INTERVAL_MINUTES: Final = 30
DEFAULT_CYCLE_DAY: Final = 1

# The recorder has no unit class for money, so cost rows are stored in the unit
# they were priced in and never converted.
CURRENCY_UNIT: Final = "USD"

ISSUE_NEWER_RATE_VERSION: Final = "newer_rate_version"
ISSUE_UNCLASSIFIED_HOURS: Final = "unclassified_hours"
ISSUE_UNKNOWN_PERIOD_LABEL: Final = "unknown_period_label"

SERVICE_RECONCILE: Final = "reconcile"
SERVICE_FINALIZE_CYCLE: Final = "finalize_cycle"
SERVICE_UNFINALIZE_CYCLE: Final = "unfinalize_cycle"

ATTR_FROM: Final = "from"
ATTR_CYCLE_START: Final = "cycle_start"
ATTR_READ_START: Final = "read_start"
ATTR_READ_END: Final = "read_end"
ATTR_SERVICE_CHARGE: Final = "service_charge"
ATTR_PCA_FACTOR: Final = "pca_factor"
ATTR_TAX: Final = "tax"
ATTR_BILL_TOTAL: Final = "bill_total"

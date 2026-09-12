"""Config flow for the NISC SmartHub integration."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
import math
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_LOCATION, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

# Home Assistant registers its selectors through `Registry[str, type[Selector]]`,
# which erases each class's config type, so the imports below arrive partially
# unknown however they are written.
from homeassistant.helpers.selector import (
    DateSelector,  # pyright: ignore[reportUnknownVariableType]
    NumberSelector,  # pyright: ignore[reportUnknownVariableType]
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,  # pyright: ignore[reportUnknownVariableType]
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,  # pyright: ignore[reportUnknownVariableType]
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    CONF_ACCOUNT,
    CONF_CYCLE_DAY,
    CONF_POLL_INTERVAL_MINUTES,
    CONF_RATE_VERSIONS,
    CONF_TARIFF,
    CONF_TOTP_SECRET,
    DEFAULT_CYCLE_DAY,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DOMAIN,
    MINIMUM_POLL_INTERVAL_MINUTES,
)
from .cycles import FIRST_CYCLE_DAY, LAST_CYCLE_DAY, cycle_day_from_input
from .smarthub import (
    AuthError,
    ClientError,
    Customer,
    MultipleMeters,
    MultipleProviders,
    ServiceLocationSummary,
    SmartHubClient,
)
from .tariff import (
    TARIFFS,
    InvalidRateVersion,
    RateVersion,
    Tariff,
    rate_version_from_data,
    rate_version_to_data,
    tariff_for_codes,
    validate_rate_version,
)

VERIFICATION_WINDOW = timedelta(days=3)


def portal_host(value: str) -> str:
    """Conform what the form calls a host to a bare host name."""
    return value.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="url")
        ),
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
        vol.Required(CONF_TOTP_SECRET): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
        vol.Required(CONF_TOTP_SECRET): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        **REAUTH_SCHEMA.schema,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_POLL_INTERVAL_MINUTES, default=DEFAULT_POLL_INTERVAL_MINUTES
        ): NumberSelector(
            NumberSelectorConfig(
                min=MINIMUM_POLL_INTERVAL_MINUTES,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        )
    }
)

SELECTION_SEPARATOR = ":"

RATE_FIELDS = (
    "on_peak_rate",
    "off_peak_rate",
    "super_off_peak_rate",
    "super_off_peak_allowance",
    "service_charge",
    "pca_factor",
    "sales_tax_rate",
    "recurring_adjustment",
)


def _cycle_day_schema(cycle_day: int) -> vol.Schema:
    """Return a schema asking only for the billing cycle's start day."""
    return vol.Schema(
        {
            vol.Required(CONF_CYCLE_DAY, default=cycle_day): NumberSelector(
                NumberSelectorConfig(
                    min=FIRST_CYCLE_DAY,
                    max=LAST_CYCLE_DAY,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            )
        }
    )


def _rate_schema(seed: RateVersion) -> vol.Schema:
    """Return a schema for one rate version, seeded from another."""
    fields: dict[Any, Any] = {
        vol.Required(
            "effective_from", default=seed.effective_from.isoformat()
        ): DateSelector()
    }
    seeded = rate_version_to_data(seed)
    for field in RATE_FIELDS:
        fields[vol.Required(field, default=seeded[field])] = NumberSelector(
            NumberSelectorConfig(step="any", mode=NumberSelectorMode.BOX)
        )
    return vol.Schema(fields)


def _cycle_schema(tariff: Tariff, seed: RateVersion) -> vol.Schema:
    """Return the billing step's schema, seeded from a published rate version."""
    fields: dict[Any, Any] = {
        vol.Required(CONF_TARIFF, default=tariff.key): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=one.key, label=one.display_name)
                    for one in TARIFFS.values()
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        **_cycle_day_schema(DEFAULT_CYCLE_DAY).schema,
        **_rate_schema(seed).schema,
    }
    return vol.Schema(fields)


class NiscSmartHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the user from credentials to one configured service location."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the flow that tunes how often this entry polls."""
        return NiscSmartHubOptionsFlow()

    def __init__(self) -> None:
        """Initialize the flow's carried state."""
        self._credentials: dict[str, str] = {}
        self._premises: dict[str, ServiceLocationSummary] = {}
        self._choices: dict[str, str] = {}
        self._account = ""
        self._location = ""
        self._tariff = next(iter(TARIFFS.values()))

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the portal host and the member's credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = portal_host(user_input[CONF_HOST])
            client = self._new_client(user_input[CONF_HOST])
            try:
                await client.login(
                    user_input[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_TOTP_SECRET],
                )
                customers = await client.user_data()
            except AuthError:
                errors["base"] = "invalid_auth"
            except ClientError:
                errors["base"] = "cannot_connect"
            else:
                self._credentials = user_input
                self._premises = _premises(customers)
                self._choices = _choices(customers)
                if not self._choices:
                    return self.async_abort(reason="no_service_locations")
                if len(self._choices) == 1:
                    return await self._async_select(next(iter(self._choices)))
                return await self.async_step_location()

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_location(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the account and service location this entry covers."""
        if user_input is None:
            return self.async_show_form(
                step_id="location", data_schema=_location_schema(self._choices)
            )
        return await self._async_select(user_input[CONF_LOCATION])

    async def async_step_cycle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the tariff, its rates, and the cycle day, then prove the poll."""
        if user_input is None:
            return self._show_cycle_form(None, {})

        errors: dict[str, str] = {}
        version = _validated_version(user_input, errors)
        cycle_day = _validated_cycle_day(user_input, errors)
        if version is not None and cycle_day is not None:
            tariff = TARIFFS[user_input[CONF_TARIFF]]
            client = self._new_client(self._credentials[CONF_HOST])
            try:
                await client.login(
                    self._credentials[CONF_EMAIL],
                    self._credentials[CONF_PASSWORD],
                    self._credentials[CONF_TOTP_SECRET],
                )
                billing = await client.billing(self._account)
                now = dt_util.utcnow()
                await client.poll_hourly(
                    self._account, self._location, now - VERIFICATION_WINDOW, now
                )
                location = await client.service_location(self._location)
            except MultipleProviders:
                return self.async_abort(reason="multiple_providers")
            except MultipleMeters:
                return self.async_abort(reason="multiple_meters")
            except AuthError:
                errors["base"] = "invalid_auth"
            except ClientError:
                errors["base"] = "cannot_connect"
            else:
                # The first version prices every hour from the floor, so it has
                # to be in effect on the connect day, read in the portal's zone.
                connect_day = billing.connect_date.astimezone(
                    dt_util.get_default_time_zone()
                ).date()
                if version.effective_from > connect_day:
                    errors["effective_from"] = "rate_version_after_connect_date"
                else:
                    return self.async_create_entry(
                        title=location.service_description,
                        data={
                            **self._credentials,
                            CONF_ACCOUNT: self._account,
                            CONF_LOCATION: self._location,
                            CONF_CYCLE_DAY: cycle_day,
                            CONF_TARIFF: tariff.key,
                            CONF_RATE_VERSIONS: [rate_version_to_data(version)],
                        },
                    )

        return self._show_cycle_form(user_input, errors)

    def _show_cycle_form(
        self, user_input: dict[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        schema = _cycle_schema(self._tariff, self._tariff.newest_published_version())
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="cycle", data_schema=schema, errors=errors)

    async def _async_select(self, selection: str) -> ConfigFlowResult:
        self._account, self._location = selection.split(SELECTION_SEPARATOR)
        await self.async_set_unique_id(f"{self._account}_{self._location}")
        self._abort_if_unique_id_configured()

        codes = self._premises[selection].active_rate_schedules
        tariff = tariff_for_codes(codes)
        if tariff is None:
            return self.async_abort(
                reason="no_matching_tariff",
                description_placeholders={"codes": ", ".join(codes)},
            )
        self._tariff = tariff
        return await self.async_step_cycle()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start over from the credentials the portal rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a fresh password and two-factor secret for the same login."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_login(
                entry.data[CONF_HOST], entry.data[CONF_EMAIL], user_input
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input, reason="reauth_successful"
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer everything about a configured entry that may still change."""
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                "add_rate_version",
                "remove_rate_version",
                "cycle_day",
                "credentials",
            ],
        )

    async def async_step_add_rate_version(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Append a dated set of rates, which every hour from its day reprices under."""
        entry = self._get_reconfigure_entry()
        configured = [
            rate_version_from_data(one) for one in entry.data[CONF_RATE_VERSIONS]
        ]
        newest = max(configured, key=lambda one: one.effective_from)
        seed = replace(newest, effective_from=dt_util.now().date())
        if user_input is None:
            return self._show_rate_form(seed, None, {})

        errors: dict[str, str] = {}
        version = _validated_version(user_input, errors)
        if version is not None:
            if any(one.effective_from == version.effective_from for one in configured):
                errors["effective_from"] = "duplicate_rate_version"
            else:
                versions = sorted(
                    [*configured, version], key=lambda one: one.effective_from
                )
                # The reload that ends this flow builds a fresh coordinator,
                # and a fresh coordinator's first run is a full pass, so the
                # hours this version governs reprice without asking for it.
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_RATE_VERSIONS: [
                            rate_version_to_data(one) for one in versions
                        ]
                    },
                )

        return self._show_rate_form(seed, user_input, errors)

    async def async_step_remove_rate_version(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Drop the newest rate version, so the one before it governs again."""
        entry = self._get_reconfigure_entry()
        versions: list[dict[str, Any]] = list(entry.data[CONF_RATE_VERSIONS])
        if len(versions) == 1:
            return self.async_abort(reason="last_rate_version")

        newest = max(versions, key=lambda one: str(one["effective_from"]))
        if user_input is None:
            return self.async_show_form(
                step_id="remove_rate_version",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "effective_from": str(newest["effective_from"])
                },
            )

        return self.async_update_reload_and_abort(
            entry,
            data_updates={
                CONF_RATE_VERSIONS: [one for one in versions if one is not newest]
            },
        )

    async def async_step_cycle_day(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Move the day the billing cycle starts on."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            cycle_day = _validated_cycle_day(user_input, errors)
            if cycle_day is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_CYCLE_DAY: cycle_day}
                )

        return self.async_show_form(
            step_id="cycle_day",
            data_schema=_cycle_day_schema(entry.data[CONF_CYCLE_DAY]),
            errors=errors,
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the login this entry uses, for the same service location."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client = self._new_client(entry.data[CONF_HOST])
            try:
                await client.login(
                    user_input[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_TOTP_SECRET],
                )
                customers = await client.user_data()
            except AuthError:
                errors["base"] = "invalid_auth"
            except ClientError:
                errors["base"] = "cannot_connect"
            else:
                if entry.unique_id in _unique_ids(customers):
                    return self.async_update_reload_and_abort(
                        entry, data_updates=user_input
                    )
                errors["base"] = "wrong_account"

        return self.async_show_form(
            step_id="credentials",
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA,
                user_input or {CONF_EMAIL: entry.data[CONF_EMAIL]},
            ),
            errors=errors,
        )

    def _show_rate_form(
        self,
        seed: RateVersion,
        values: dict[str, Any] | None,
        errors: dict[str, str],
    ) -> ConfigFlowResult:
        schema = _rate_schema(seed)
        if values is not None:
            schema = self.add_suggested_values_to_schema(schema, values)
        return self.async_show_form(
            step_id="add_rate_version", data_schema=schema, errors=errors
        )

    async def _async_login(
        self, host: str, email: str, credentials: dict[str, Any]
    ) -> str | None:
        """Return the error key a live login produced, or nothing."""
        client = self._new_client(host)
        try:
            await client.login(
                email, credentials[CONF_PASSWORD], credentials[CONF_TOTP_SECRET]
            )
        except AuthError:
            return "invalid_auth"
        except ClientError:
            return "cannot_connect"
        return None

    def _new_client(self, host: str) -> SmartHubClient:
        return SmartHubClient(
            async_get_clientsession(self.hass), host, dt_util.get_default_time_zone()
        )


class NiscSmartHubOptionsFlow(OptionsFlow):
    """How often the integration polls the portal."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the poll interval and restart the entry on it."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                interval = poll_interval_from_input(
                    user_input[CONF_POLL_INTERVAL_MINUTES]
                )
            except ValueError:
                errors[CONF_POLL_INTERVAL_MINUTES] = "invalid_poll_interval"
            else:
                options = {CONF_POLL_INTERVAL_MINUTES: interval}
                # The coordinator reads the interval once, when it is built, so
                # the options are written here and the entry reloaded onto them
                # rather than waiting for a reload nobody asked for.
                self.hass.config_entries.async_update_entry(entry, options=options)
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, user_input or entry.options
            ),
            errors=errors,
        )


def poll_interval_from_input(value: Any) -> int:
    """Conform a form's poll interval in minutes, or refuse it.

    A number selector hands back a float, and a fractional minute, an infinite
    one, or one under the minimum is a typo rather than an interval to round.
    """
    try:
        minutes = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError("the poll interval is not a number") from err
    if (
        not math.isfinite(minutes)
        or not minutes.is_integer()
        or minutes < MINIMUM_POLL_INTERVAL_MINUTES
    ):
        raise ValueError(
            f"the poll interval {value!r} is not a whole number of minutes"
        )
    return int(minutes)


def _unique_ids(customers: list[Customer]) -> set[str]:
    """Return the unique id of every service location a login can see."""
    return {
        f"{customer.account}_{location.id}"
        for customer in customers
        for location in customer.locations
    }


def _validated_version(
    user_input: dict[str, Any], errors: dict[str, str]
) -> RateVersion | None:
    try:
        return validate_rate_version(user_input)
    except InvalidRateVersion as err:
        errors[err.field] = err.reason
        return None


def _validated_cycle_day(
    user_input: dict[str, Any], errors: dict[str, str]
) -> int | None:
    try:
        return cycle_day_from_input(user_input[CONF_CYCLE_DAY])
    except ValueError:
        errors[CONF_CYCLE_DAY] = "invalid_cycle_day"
        return None


def _premises(customers: list[Customer]) -> dict[str, ServiceLocationSummary]:
    return {
        f"{customer.account}{SELECTION_SEPARATOR}{location.id}": location
        for customer in customers
        for location in customer.locations
    }


def _choices(customers: list[Customer]) -> dict[str, str]:
    return {
        f"{customer.account}{SELECTION_SEPARATOR}{location.id}": (
            f"{location.address.line_one}, {location.address.city} ({customer.account})"
        )
        for customer in customers
        for location in customer.locations
    }


def _location_schema(choices: dict[str, str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_LOCATION): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=value, label=label)
                        for value, label in choices.items()
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            ),
        }
    )

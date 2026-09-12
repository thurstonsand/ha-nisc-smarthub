# Home Assistant Development Rules

Harvested from [`jpawlowski/hacs.integration_blueprint`](https://github.com/jpawlowski/hacs.integration_blueprint).

## Contracts that hold everywhere

These are the ones an agent typically breaks _before_ it realises a rule applies.

- **Entities → Coordinator → source.** Never skip a layer; entities read `coordinator.data` and never reach past it. The source is usually an API client in `api/`, but it can equally be a state listener, a file, or a computation.
- **Register service actions in `async_setup()`**, not `async_setup_entry()` (Quality Scale rule `action-setup`).
- **Never add `device_trigger.py`, `device_condition.py` or `device_action.py`.** Device automations are frozen upstream — existing ones keep working, new ones are not accepted. Older integrations are full of them, so this is a pattern to recognise and not copy. Use the trigger and condition platform instead.
- **A unique ID is a serial number, MAC, device ID or account ID** — never an IP address, hostname, URL, an email address, a username, or a name the user chose.
- **Entity metadata comes from `EntityDescription` + `translation_key`** — never a hardcoded `name=` or `icon=`.
- **Coordinator failures raise**: `ConfigEntryAuthFailed` (triggers reauth), `UpdateFailed` (retry), `ConfigEntryNotReady` during setup (retry later), or `ConfigEntryError` when the failure will not resolve on its own — a closed account, an unsupported portal — which stops the retry loop instead of spinning forever. Do not log `ConfigEntryNotReady` manually; HA already logs it at debug level.
- **Diagnostics must call `async_redact_data()`** for credentials, tokens, location and personal data. Here that also covers the account number, the service location id and the meter number.
- **YAML configuration is deprecated** for integrations talking to devices or services (ADR-0010) — config flow only.
- **Changing the shape of `entry.data`** requires a `VERSION`/`MINOR_VERSION` bump and `async_migrate_entry()`.
- **Runtime state lives in `entry.runtime_data`**, never `hass.data[DOMAIN]`.
- **While Home Assistant runs, ask it — do not read `config/.storage/`.** Those files are written 1–180 seconds after the change they describe and hold no live state at all. With Home Assistant stopped it is the other way round, and then they are the only accurate source. Never write into `config/.storage/` while it runs; the next save discards the edit.

### Device registry ownership (Home Assistant 2026.8+)

Model priors are usually wrong here, and the rules apply across migrations, diagnostics, listeners and tests.

Every device is owned by exactly one config entry and at most one config subentry. Identifiers and connections are unique only within their owning entry — never assume they are globally unique.

- Scope lookups with `async_get_device_by_identifier(identifier, config_entry_id)` or `async_get_device_by_connection()`; the unscoped `async_get_device()` is out.
- Inside an entity, use `self.device_entry` rather than looking the device up again.
- Never attach this config entry to a device owned by another integration.
- Use `via_device_id`, not `via_device`, for a parent relationship. Passing both raises at runtime, so a half-finished migration fails loudly.
- Do not rely on the composite-device compatibility shims — they are scheduled for removal in HA Core 2027.8.

## Integration structure

**Package organization — do not create packages outside this list:**

- `api/` — API client and exceptions
- `coordinator/` — data update coordinator
- `config_flow_handler/` — config flow, options, `validators/`, `schemas/`
- `entity/` — base entity classes
- `entity_utils/` — entity helpers (device info, state formatting)
- `sensor/` — the entity platform, one entity class per file
- `utils/` — integration-wide utilities

Top-level modules beside these: `config_flow.py` (a discovery shim Home Assistant requires at the integration root), `diagnostics.py`, `const.py`.

`helpers/`, `common/`, `shared/`, `lib/` and any other new top-level package need explicit approval — use `utils/` or `entity_utils/` instead.

`PLATFORMS` is defined in `__init__.py`.

**Keep files focused** — roughly 200–400 lines, ~500 before refactoring, one class per file for entities. Split by extracting helpers, moving entity classes into their own files, or grouping constants.

**Naming:** files `snake_case.py`, classes `PascalCase` with the integration's class prefix, functions `snake_case`, constants `UPPER_SNAKE_CASE`.

### This is a custom integration, not a Core one

It follows Core patterns for quality, but implementation decisions have more room.

**Third-party library or own client?** Prefer a maintained PyPI library that fits. Write a client instead when the service speaks a simple REST API, or when the available libraries are unmaintained, bloated or badly designed. SmartHub's API is undocumented and vendored per co-op, which is why the client lives here — with the consequence that it would have to be extracted before this could ever be submitted to Core.

**Aim for Silver or Gold on the Quality Scale.** Always implement type hints, async I/O, proper error handling, actions registered in `async_setup()`, redacted diagnostics and device info. Add config flow validation and reauth where they apply. Multiple config entries, discovery, YAML import and exhaustive coverage may be deferred.

## Coordinator and API client

### Layering

- **Entities:** read `coordinator.data` only, never call the API.
- **Coordinator:** calls the API, transforms data, handles errors and timing.
- **API client:** HTTP communication, auth, exception translation.

✅ `self.coordinator.data.temperature` (in entity properties)

❌ `await self.api_client.get_data()` (never fetch directly in entities)

### API client rules

**Session management:**

- MUST accept an `aiohttp.ClientSession` parameter in `__init__`.
- NEVER create a session (`aiohttp.ClientSession()`) in the client.
- The session comes from `async_get_clientsession(hass)` in `__init__.py`.

**Timeout handling:** use `asyncio.timeout()`, not `async_timeout`. Every request needs one; a bare `await` is not acceptable.

```python
async with asyncio.timeout(10):
    response = await self._session.get(url)
```

**Return values:**

- Return the raw API response; let the coordinator transform it for entities.
- **Mirror the API's own structure**, even where it is badly designed or contains a typo. A client that "improves" the shape hides what the service actually returns, and the next reader cannot match it against observed responses.
- **Never convert units.** That decides precision and rounding on the caller's behalf; set `native_unit_of_measurement` on the entity and let Home Assistant convert.
- Implement pagination here — fetch everything and hand the coordinator a complete dataset.

**Authentication:** the auth layer authenticates; it does not **store**. Persisting tokens is the config entry's job. Return tokens as JSON-serializable values so they survive being written into `entry.data`.

**Do not** implement retry logic here, and do not catch `TimeoutError` / `aiohttp.ClientError` in the coordinator — the coordinator base class already handles both.

### Exception hierarchy

Define in `api/__init__.py`:

- `{ClassPrefix}ApiClientError` (base)
- `{ClassPrefix}ApiClientCommunicationError` (network, timeout, HTTP errors)
- `{ClassPrefix}ApiClientAuthenticationError` (401, 403, invalid credentials)

**Mapping:** HTTP 401/403 → auth, timeout / `ClientError` → communication.

### Error handling in `_async_update_data()`

Translate API exceptions into Home Assistant ones here, and only here:

| API exception         | Raise                     | Home Assistant behaviour |
| --------------------- | ------------------------- | ------------------------ |
| `AuthenticationError` | `ConfigEntryAuthFailed`   | Triggers reauth flow     |
| `CommunicationError`  | `UpdateFailed("message")` | Retry with backoff       |

Use `raise ... from err`. Pass the error message to the exception constructor and **do not log** setup or update failures manually — Home Assistant does it, and manual logging buries the real error in repetition. Normal operation logging (debug/info) is still appropriate.

The four failures no mapping table can express, and the root of most availability bugs:

- **Signalling failure by returning** `None` or an empty dict instead of raising. Entities then show `unknown` forever instead of going unavailable, and nothing retries.
- **A broad `except Exception`** that swallows the auth error, so reauth never triggers and the entry just looks broken.
- **Logging on every failed poll.** The coordinator logs the first failure and then stays quiet by design (`log-when-unavailable`, Silver).
- **`ConfigEntryNotReady` raised outside setup**, or `async_config_entry_first_refresh()` called outside setup. Raised from a platform's `async_setup_entry` it is inert — by then the config entry setup has already completed and cannot catch it.

### Data transformation

`_async_update_data()` fetches the raw payload and transforms it into a shape entities read by key: entities read `coordinator.data["usage_today"]`, not `coordinator.data["series"][0]["values"]["USAGE"]`.

Expensive one-off work — fetching account metadata, resolving the service location and meter — belongs in `_async_setup()`, which runs once before the first refresh, not in `_async_update_data`.

### Update interval

```python
super().__init__(hass, LOGGER, config_entry=entry, name="...", update_interval=timedelta(minutes=30))
```

**Always pass `config_entry=`.** Omitting it makes the coordinator fall back to a ContextVar, which for a custom integration is set to ignore the problem — so it fails silently rather than loudly.

The Bronze `appropriate-polling` rule is about honesty: a cloud service that publishes hourly data does not want a 30-second poll. Read the interval from `entry.options` so the user can tune it.

**Values that change with the clock need a scheduler, not a shorter interval.** A value derived from a payload that covers a defined period changes on the hour; the answer is one fetch per validity window plus `async_track_point_in_utc_time` / `async_track_time_change` to recompute locally — not polling every minute so the value happens to flip in time. Polling for something the integration can compute is also what makes `appropriate-polling` look violated.

### First refresh

In `async_setup_entry()` in `__init__.py`, call `await coordinator.async_config_entry_first_refresh()`. If `_async_update_data()` raises `UpdateFailed`, the coordinator raises `ConfigEntryNotReady` automatically.

**When setup should not be retried at all**, use `await coordinator.async_refresh()` instead — it does not raise, so the entry loads with entities in an unavailable state rather than going into the retry loop.

### Caching

**In memory, to fetch less often than entities update.** Hold the payload and its timestamp on the coordinator and return the cached copy from `_async_update_data()` while the TTL holds. Entities then update at `update_interval` while the API is called far less often.

**Persisted, so the integration works without a connection at startup.** Home Assistant restarts without internet more often than one would think — after a power cut it is regularly up before the router is. The reflex, `async_config_entry_first_refresh()`, raises `ConfigEntryNotReady` when that first fetch fails, and then **no entity exists at all**: exactly when the user most wants to see the last known values, the integration shows nothing.

First decide whether the cached payload is still true, because this is what separates honest caching from lying about the source:

| The payload…                                                                            | On a cold start with no network            |
| --------------------------------------------------------------------------------------- | ------------------------------------------ |
| Covers a defined period — yesterday's hourly usage, a billing cycle already read        | Restore it. It is complete and still valid |
| Is a point-in-time reading — a freshness timestamp, a cycle-to-date figure still moving | Do **not** restore it. It is stale         |

For the first kind:

- Persist the payload with `homeassistant.helpers.storage.Store` when a fetch succeeds, and load it in `async_setup_entry` before the coordinator's first refresh.
- **Return the cached payload from `_async_update_data()` instead of raising `UpdateFailed`**, as long as it is still inside its validity window. This is the part that actually works: `CoordinatorEntity.available` is exactly `coordinator.last_update_success`, so raising `UpdateFailed` and merely leaving `coordinator.data` populated makes every entity unavailable and shows the user nothing.
- Once the window has passed, raise `UpdateFailed` as normal.
- Log the fallback once at `info` level, so "still on cached data" is visible without spamming every poll.

The Bronze `test-before-setup` rule is satisfied either way: setup still fails loudly when there is nothing valid to fall back on. What changes is that a valid cache counts as "we can work".

## Config flow

The config flow is the only supported way to configure a service integration (ADR-0010). Everything the user can change lives here, and mistakes are expensive: `unique_id` and the shape of `entry.data` are effectively permanent.

### File organization

```text
config_flow_handler/
├── __init__.py          # exports
├── config_flow.py       # user, reauth, reauth_confirm, reconfigure steps
├── options_flow.py      # post-setup options
├── handler.py           # logic shared between the flows above
├── schemas/
│   ├── config.py        # voluptuous schemas for setup steps
│   └── options.py       # voluptuous schemas for the options flow
└── validators/
    ├── credentials.py   # "can we actually talk to it" checks
    └── sanitizers.py    # input normalisation
```

Create these when something needs them rather than leaving empty scaffolding behind. **MUST** maintain `config_flow.py` at the integration root (a hassfest requirement) that imports from the package.

### Result types

Every step method returns one of: `FORM` (`async_show_form`), `CREATE_ENTRY` (`async_create_entry`), `ABORT` (`async_abort`), `SHOW_MENU`, `EXTERNAL_STEP`, or `SHOW_PROGRESS`. For a long validation — a SmartHub login with TOTP is one — `async_show_progress(step_id, progress_action, progress_task)` then `async_show_progress_done(next_step_id)`; report a fraction with `self.async_update_progress(0.5)`, and while the task is still running call `async_show_progress` again rather than starting a second one.

### Form schemas

- Required keys first, optional second.
- An optional key's default must be a **valid value** — `vol.Optional(CONF_X, default=None): cv.string` is wrong; use `default=""`.
- Reach for the specific validator before `cv.string`: `cv.port`, `cv.url`, `cv.positive_int`, `cv.small_float`, `cv.time_zone`, `cv.slug`.
- Give every schema field a `selector.*` and a default. A bare `vol.Coerce(int)` renders as an untyped box and is a review blocker.
- Give every field both a `data` and a `data_description` translation key.
- Pre-fill with `self.add_suggested_values_to_schema(schema, entry.data)` on reconfigure and options.
- Group with `section()` — one level only, and note that a section **nests the submitted data**: `{"account": …, "advanced": {"scan_interval": …}}`.
- **Do not use `SchemaConfigFlowHandler`.** It writes every value into `options`, which contradicts the data/options split, so it cannot hold credentials.

```python
vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=60, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX),
),
```

**Browser autofill:** recognised field names (`username`, `password`) are auto-mapped; otherwise set `TextSelectorConfig(autocomplete="username")`.

### Validation and errors

Return an errors dict keyed by translation key — `errors={"base": "cannot_connect"}`. Common keys: `cannot_connect`, `invalid_auth`, `already_configured`, `unknown`. Pattern: try validation → catch exceptions → set errors → re-show the form. **MUST log unexpected exceptions:** `_LOGGER.exception("Unexpected exception")`.

Do not do blocking I/O or long retries inside a flow step; validate with a short timeout.

Do not give an optional free-text field `default=None` — voluptuous injects that default when the field is left empty and the selector then rejects it, making the form unsubmittable. Carry the current value in `description={"suggested_value": ...}` instead.

### Data versus options

Where a value lives is decided once and changing it later requires a migration.

| `entry.data`                                            | `entry.options`                                           |
| ------------------------------------------------------- | --------------------------------------------------------- |
| Identity and connection: portal host, credentials, TOTP | Behaviour the user may tune later: poll interval, rates   |
| Anything needed to establish the connection at all      | Anything safe to change without re-validating credentials |

**MUST:**

- Keep credentials in `entry.data` only — never in `entry.options`, never in the entry title.
- Reuse `CONF_*` names from `homeassistant.const` where one exists; otherwise define them in `const.py`.
- Tolerate entries created before a field existed — supply a default at read time or migrate.

**Consumers:** the coordinator reads `entry.options`, the API client reads `entry.data`.

### Step names and unique IDs

Reserved system steps: `user`, `reauth`, `reconfigure`, `import`. `discovery` is a deprecated step name — never implement `async_step_discovery`. A reauth flow starts with `source`, `entry_id` and `unique_id` in `self.context`, and reauth and reconfigure set `title_placeholders` to `{"name": <entry title>}` for you.

For a cloud service the unique ID is the account id, when it is guaranteed collision-free. Set it with `await self.async_set_unique_id(account_id)` and call `self._abort_if_unique_id_configured()`. Normalize a username or email to lowercase, and only use one when nothing better exists. Never an IP address, a user-changeable name, or a URL.

### Version and migration

`VERSION` for restructuring, `MINOR_VERSION` for additive changes; both default to `1`, and you set them only when implementing a migration. A newer minor version still loads without `async_migrate_entry`. A major bump means the entry **fails to load** if the user downgrades Home Assistant — that asymmetry is the reason to prefer a minor bump whenever the change is additive.

```python
async def async_migrate_entry(hass: HomeAssistant, entry: {ClassPrefix}ConfigEntry) -> bool:
    """Migrate an old config entry."""
    if entry.version > 1:
        # Downgrade from a future version — refuse rather than corrupt data.
        return False
    if entry.version == 1 and entry.minor_version < 2:
        data = {**entry.data, CONF_CYCLE_START_DAY: DEFAULT_CYCLE_START_DAY}
        hass.config_entries.async_update_entry(entry, data=data, minor_version=2)
    return True
```

Migrations must be idempotent and must never delete a key they do not understand. Cover every migration with a test.

### Entry lifecycle

- **`async_setup_entry(hass, entry)`** — forward platforms, return `True`, raise `ConfigEntryNotReady` / `ConfigEntryAuthFailed`.
- **`async_unload_entry(hass, entry)`** — always implement it. `entry.async_on_unload()` callbacks are not a substitute, and they also run when `async_setup_entry` raises.
- **`async_remove_entry(hass, entry)`** — optional; cleanup after deletion.
- **NEVER mutate `ConfigEntry` directly** — use `hass.config_entries.async_update_entry()`.
- Outside setup and the coordinator, `ConfigEntryAuthFailed` does nothing — call `entry.async_start_reauth(hass)`.

### Adding a config option

An options flow that applies its change by reloading the entry calls `hass.config_entries.async_schedule_reload` itself and must not also register an update listener. In 2026.9 `async_update_reload_and_abort` warns about integrations that do both, and 2026.12 removes the combination.

1. **Decide data or options** using the table above. This is the one irreversible choice here.
2. Add the `CONF_*` key to `const.py`.
3. Add the field to `schemas/config.py` or `schemas/options.py` with a selector and a default.
4. Consume it: coordinator reads `entry.options`, client reads `entry.data`.
5. Add the `data` and `data_description` translation keys.
6. **Handle existing entries** that predate the key — a default at read time, or a `MINOR_VERSION` bump plus migration. This is the step that gets forgotten and breaks upgrades.

## Entities

### Base class and descriptions

**MUST inherit from:** `(SensorEntity, {ClassPrefix}Entity)` — the integration's base entity class from `..entity`, order matters for MRO. The base provides coordinator integration, device info, unique ID, attribution and entity naming; you implement `native_value` and friends.

`{entry_id}_{key}` is the documented **unique ID of last resort**. If the account exposes a stable id, switch to it **before the first release** — afterwards it is a breaking change needing a registry migration.

**Define descriptions at module level:** `ENTITY_DESCRIPTIONS: tuple[SensorEntityDescription, ...]`.

- `key` — used in the unique ID, must match the coordinator data key. Never rename it after release.
- `translation_key` — the entity name comes from `translations/en.json`. **NEVER set `name=` to a string**; the base entity sets `_attr_has_entity_name = True`, and a hardcoded name breaks localisation (`entity-translations`). Exception: on `sensor`, an entity whose `device_class` already produces the wanted name needs **no** `translation_key`.
- **Set `device_class` whenever one fits** — it drives unit conversion, icons and voice assistants.
- **Set `state_class` on every numeric measurement** — without it there are no long-term statistics. But `MEASUREMENT` is **invalid** with a `device_class` of `ENERGY`, `MONETARY`, `GAS`, `WATER`, `VOLUME`, `DATE`, `TIMESTAMP` or `ENUM` — meters take `TOTAL` or `TOTAL_INCREASING`.
- **NEVER set `icon=`** — icons belong in `icons.json` (`icon-translations`).

**Value extraction:** subclass the description dataclass with a `value_fn` rather than branching on `key` in the entity.

```python
@dataclass(frozen=True, kw_only=True)
class {ClassPrefix}SensorEntityDescription(SensorEntityDescription):
    """Describes a sensor and how to read it from coordinator data."""

    value_fn: Callable[[dict[str, Any]], StateType]
```

**Entity categories:** `None` for primary functionality, `EntityCategory.DIAGNOSTIC` for uptime, freshness and errors, `EntityCategory.CONFIG` for settings. Anything a user would not put on a dashboard is diagnostic; if it is also noisy, choose between `entity_registry_enabled_default=False` (not created at all) and `_attr_entity_registry_visible_default = False` (created and automatable, just off dashboards). Note the `_attr_` prefix on the class form: writing the bare property name silently does nothing.

### Reading coordinator data

**MUST use the coordinator only:** `self.coordinator.data.get(self.entity_description.key)`. No `self.coordinator.client`, no `await api_call()` in an entity.

**Missing data is `unknown`, not `unavailable`.** The two are not interchangeable: `unavailable` hides the entity and breaks templates that read its state.

- The poll succeeded but one field is absent → return `None` from `native_value`. The state becomes `unknown` and the entity stays usable.
- The whole source is gone → the base `CoordinatorEntity` already covers that. Only override `available` when a missing key really means a missing sub-device.

**Never log in a property getter** — they are called on every state read.

### Attributes

**Prefer a second entity.** Every attribute is written to the recorder database on every state change, so a frequently changing attribute on a frequently changing entity multiplies database growth. A value worth showing is usually worth its own sensor.

If attributes are still right, use `extra_state_attributes` and exclude the volatile ones from history with a **class-level** `_unrecorded_attributes: frozenset[str]` (instance attributes are ignored). **NEVER override `state_attributes` or `capability_attributes`** — those are reserved for base platform components.

### Device info

The base entity supplies `identifiers`, `name`, `manufacturer` and `model`. Fill in whatever else the source actually knows; a `DeviceInfo` must match one of the registry's shapes, never a partial mix. For a cloud account, set `entry_type=DeviceEntryType.SERVICE` — it is not a physical device. `configuration_url` can point at the co-op's SmartHub portal.

Device info is only read when the entity is set up from a **config entry** and has a `unique_id`. No unique ID means `device_info` and every registry property are silently ignored.

### `PARALLEL_UPDATES`

Home Assistant reads `PARALLEL_UPDATES` from the platform module, so the platform `__init__.py` declares it as a module-level literal. Do not import it from `const.py`. A read-only platform takes `0`; everything that writes to the service takes `1`. Missing it is a quality scale failure (`parallel-updates`).

```python
# Read-only platform: the coordinator already serializes the fetch.
PARALLEL_UPDATES = 0
```

### Platform setup

`async_setup_entry()` creates entities from the descriptions, reading the coordinator off `entry.runtime_data`:

```python
async_add_entities(EntityClass(entry.runtime_data.coordinator, desc) for desc in ENTITY_DESCRIPTIONS)
```

For a brand-new platform, add the `Platform.<NAME>` member to `PLATFORMS` in `__init__.py` and nothing else — `async_forward_entry_setups` and `async_unload_platforms` already cover it.

**Event subscriptions:** subscribe in `async_added_to_hass()` and release every subscription via `self.async_on_remove(...)` (`entity-event-setup`). Call `await super().async_added_to_hass()` first. Never register a reference to an entity object outside `async_added_to_hass`: a disabled entity is never added, and the reference would dangle.

### Something happened, rather than something is

For anything that **occurs** instead of **holds**, reach for an `event` entity, not a sensor. **Do not represent an event as entity state** — a binary sensor that is `on` for 30 seconds after something happened invents a duration the source never reported and loses a second occurrence inside the window.

## Statistics, recorder and diagnostics

### What the recorder sees

- Long-term statistics exist only for entities with a `state_class`; a numeric sensor without one gets history and no statistics.
- Units come from `homeassistant.const` constants, never hardcoded strings, and the integration does not convert. Set `native_unit_of_measurement` and let Home Assistant convert according to `hass.config.units`.
- Timestamps are UTC, via `dt_util.utcnow().isoformat()`. Never relative time ("2 hours ago") in a state or attribute.
- Event and attribute payloads must be **JSON-serializable**. A `datetime` or a dataclass breaks the recorder and the WebSocket API. Fired events land in the recorder database, so keep the payload small.
- Changing a `state_class`, a unit, or an entity ID discards or corrupts statistics history that cannot be recovered. One case is recoverable: when only the **spelling** of a unit changes (`"KWh"` to `UnitOfEnergy.KILO_WATT_HOUR`), declare the pair equivalent with `recorder.async_custom_equivalent_units` instead of letting the statistics break.

External statistics, the reconcile window and the sum-continuity rules this integration lives by are project design, not blueprint material; they belong to the design doc and `CONTEXT.md`.

### Diagnostics

`diagnostics.py` runs everything through `async_redact_data()` with a `TO_REDACT` set that actually covers the payload — re-check it whenever the API response shape changes.

```python
from homeassistant.helpers.redact import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    "api_key",
    "token",
    "refresh_token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": async_redact_data(entry.data, TO_REDACT),
        "entry_options": async_redact_data(entry.options, TO_REDACT),
        "coordinator_data": coordinator.data,
    }
```

Always redacted: passwords, API keys and tokens, OAuth credentials, TOTP secrets, location data, personal information, and here the account number, service location id and meter number. **When in doubt, redact it.** Diagnostics get pasted into public issues.

`async_get_device_diagnostics(hass, entry, device)` is a second, optional entry point with identical redaction rules.

## Translations and icons

`translations/en.json` is the source of truth for every string a user sees. Home Assistant falls back to the raw key when a string is missing, so a missing key is a visible bug, not a cosmetic one.

**Update `en.json` only**, and only when asked or at a feature milestone. Never touch another language file without asking — code works without translations, so business logic comes first.

**NEVER use `[%key:...%]` references.** They are a Home Assistant **Core** build-time feature: Core compiles `strings.json` into `translations/en.json` and resolves the references on the way; a custom integration has no such build step, so its `translations/*.json` is served exactly as written. Write out the full English text for every key, even when it duplicates another key or a Core string. The symptom when this is wrong: the config flow shows raw keys instead of translated labels.

This repository still authors `strings.json` (hassfest reads it) and ships an identical `translations/en.json`; `mise run strings:check` fails the build if they drift or if a `[%key:...%]` reference appears.

**Placeholders** use `{variable}` and their names must match the code exactly. Do not put single quotes around a placeholder inside a string value (`"Service '{service}' is unavailable"`) — it is untranslatable across languages and fails validation. Escaped double quotes are fine.

### Which keys a change needs

| You added…                         | Keys required                                                                            |
| ---------------------------------- | ---------------------------------------------------------------------------------------- |
| An entity with a `translation_key` | `entity.sensor.<translation_key>.name`                                                   |
| An enum sensor                     | …plus `entity.sensor.<key>.state.<value>` for **every** possible value                   |
| An entity attribute                | `entity.sensor.<key>.state_attributes.<attr>.name`                                       |
| A config flow field                | `config.step.<step>.data.<field>` **and** `config.step.<step>.data_description.<field>`  |
| A new `errors["base"] = "x"`       | `config.error.x`                                                                         |
| A new `async_abort(reason="x")`    | `config.abort.x`                                                                         |
| An options flow field              | `options.step.init.data.<field>` and `options.step.init.data_description.<field>`        |
| A raised `HomeAssistantError`      | `exceptions.<translation_key>.message`                                                   |
| An issue                           | `issues.<issue_id>.title`, plus **either** `.description` **or** `.fix_flow.*`, not both |

Two rules that catch most mistakes: entity names live under `entity.*.name` while form field labels live under `step.*.data.*`, and they are not interchangeable; and `data_description` is not optional in practice — its absence is what makes a setup form feel unfinished.

### Writing the strings

- Sentence case for names and labels ("Cycle-to-date usage", not "Cycle-To-Date Usage"). Proper nouns and capitalised abbreviations keep their casing.
- **Keys** are `snake_case` — including translated state values. Only the values are prose.
- No trailing period on `name` and `data` labels; full sentences with a period for `description` and `data_description`.
- Do not repeat the device or integration name in an entity name — `_attr_has_entity_name = True` means Home Assistant prefixes it already.
- Error messages say what happened and what to do, and never leak credentials, tokens or raw stack traces.
- Use the word the project already settled on. `CONTEXT.md` decides the wording here, because renaming an entity later is a breaking change.
- Do not add keys "for later", and do not reformat the whole file while adding one key.

### Translated exceptions

```json
{
  "exceptions": {
    "poll_failed": { "message": "Could not read usage: {error}" }
  }
}
```

```python
raise HomeAssistantError(
    translation_domain=DOMAIN,
    translation_key="poll_failed",
    translation_placeholders={"error": str(err)},
)
```

### `icons.json`

Entity icons belong in `custom_components/nisc_smarthub/icons.json`, not in `EntityDescription(icon=...)`. **Do not give an entity an icon its device class already provides** — an energy sensor and a monetary sensor are iconed correctly already, and overriding them makes the integration look inconsistent with every other one. A key can also carry `range` (pick by value, ascending, highest bound less than or equal to the current value; `state` wins if both are present) and `state_attributes`.

```json
{
  "entity": {
    "sensor": {
      "cycle_usage": { "default": "mdi:transmission-tower" }
    }
  }
}
```

## Tests

Home Assistant tests are integration tests by nature: you load a real config entry into a real `hass` instance and assert on `hass.states` — not on Python objects.

**Test through core interfaces, not integration internals.** The point is not purity — it is that a test which reaches into the integration has to be rewritten every time the integration is refactored, so it stops being a safety net exactly when one is needed.

✅ `hass.config_entries.async_setup`, `MockConfigEntry`, `hass.states`, `hass.services`, `entry.state`, the device and entity registries

❌ Direct entity instantiation, reading entity properties, reaching into `entry.runtime_data`

### Layout

`tests/` mirrors `custom_components/nisc_smarthub/`:

```text
tests/
├── conftest.py                # shared fixtures
├── test_init.py               # setup, unload, reload, migration
├── test_config_flow.py        # user, reauth, reconfigure, options
├── test_diagnostics.py        # snapshot + redaction
├── sensor/test_cycle_usage.py
└── snapshots/                 # syrupy .ambr files, committed
```

### Bootstrap `tests/conftest.py`

Custom integrations are **not** loaded by default in tests, so the first thing any new test file needs is this:

```python
"""Shared fixtures for nisc_smarthub tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nisc_smarthub.const import DOMAIN
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the custom integration in every test."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the integration."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="SmartHub",
        data={CONF_USERNAME: "test-user", CONF_PASSWORD: "test-password"},
        unique_id="test-unique-id",
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_client: AsyncMock,
) -> MockConfigEntry:
    """Set up the integration and return the loaded entry."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
```

Patch the client where it is **imported**, not where it is defined. `autospec=True` makes the mock fail when the real signature changes, which is the whole point.

### Patterns

**Setup failure** — parametrise the client error against the entry state it should produce:

```python
@pytest.mark.parametrize(
    ("error", "expected_state"),
    [
        ({ClassPrefix}ApiClientCommunicationError, ConfigEntryState.SETUP_RETRY),
        ({ClassPrefix}ApiClientAuthenticationError, ConfigEntryState.SETUP_ERROR),
    ],
)
async def test_setup_failures(hass, mock_config_entry, mock_api_client, error, expected_state) -> None:
    """Client failures map onto the right entry state."""
    mock_api_client.async_get_data.side_effect = error
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is expected_state
```

**Config flow** — parametrise the error branches (`invalid_auth`, `cannot_connect`, `unknown`) and always assert that the flow **recovers**: show the form again with the error, then succeed on the second attempt. `config-flow-test-coverage` (Bronze) expects every step and every abort reason to be exercised.

**Time-driven updates** — the `freezer` fixture, never `time.sleep`:

```python
freezer.tick(DEFAULT_SCAN_INTERVAL)
async_fire_time_changed(hass)
await hass.async_block_till_done()
```

**Registry assertions** — device by scoped lookup (`async_get_device_by_identifier((DOMAIN, id), entry.entry_id)`), entity by `er.async_get(hass).async_get("sensor.x")`, and lifecycle `async_setup()` → `LOADED`, `async_unload()` → `NOT_LOADED`. Never the deprecated unscoped `async_get_device()`.

**Snapshots** (syrupy) are for large stable structures — diagnostics, entity registry dumps. They complement functional assertions; they do not replace them. A snapshot asserts "unchanged since I recorded it", which assumes the recording was right; to check that an entity goes unavailable on an API error, assert that specific state. Read the diff before committing an `.ambr` file — an accepted snapshot of a bug is worse than no test.

### Mocking

✅ Mock external APIs, network calls, time-dependent operations. `patch.object()` for success cases, `side_effect` for errors.

❌ Do not mock Home Assistant internals or our own integration code. Do not mock `aiohttp` at the transport level when mocking the client is enough — you end up testing your mock.

### When a test is required

Behavioural change, bug fix or regression → add a proportionate test. Documentation-only or formatting-only → none. If a test is impractical, say so explicitly and describe the residual risk. Never claim coverage that does not exist, and never describe a test as passing without running it.

Coverage priorities, highest value first: config flow branches → setup/unload/migration → coordinator error translation → entity state and availability → diagnostics redaction. Chase behaviour, not a percentage.

**Warnings are errors.** `pyproject.toml` sets `filterwarnings = ["error"]`, so a `DeprecationWarning` fails `mise run test`. That is the intended early-warning system — fix the deprecation rather than adding an ignore. The exception is a warning Home Assistant Core raises about its own use of a library it pins, which no change here can fix; key such an ignore to the exact message rather than the category, and say where it comes from.

## Breaking changes

Users have automations, dashboards, scripts and long-term statistics wired to this integration's entity IDs, unique IDs, states and statistic IDs. Breaking any of them is a real cost to real people, and the integration cannot see who it broke.

### Recognise it

Treat as breaking: changing entity IDs, unique IDs or `EntityDescription.key`; changing the structure of `entry.data` or `entry.options`; changing a state value, unit, `device_class`, `state_class` or attribute name; renaming or changing the signature of a service action; removing or renaming a config option — including one you believe is unused; raising the minimum Home Assistant version; removing an entity or a device.

### Warn before you implement

Never make a breaking change silently, and never as an incidental part of a larger task. State it plainly and stop:

> ⚠️ This changes the entity ID format from `sensor.device_name` to `sensor.device_name_sensor`. Existing automations and dashboards will break. Should I proceed, or would you prefer a migration path?

Wait for an explicit answer. A prior approval for one breaking change is not approval for the next. Record it with a `BREAKING CHANGE:` footer either way.

### Which side of 1.0.0 is this?

**Before `1.0.0`, breaking is usually the right answer** — the goal is a settled code base, not compatibility code wrapped around a shape nobody has committed to yet. Applying the post-1.0 rules early is its own kind of damage.

- **What still needs asking is whether to build the migration**, not whether to break. Never write `async_migrate_entry` or bump `VERSION` / `MINOR_VERSION` unprompted.
- **Recommend.** "The cleanest fix is to rename the key and let existing test entries be recreated; a migration would cost ~40 lines we would then maintain" is the useful form. Not "shall I migrate?"
- **Keep the `BREAKING CHANGE:` footer anyway.** HACS installs `0.x` versions too, and anyone testing early deserves a changelog entry rather than a silent surprise.

This is also the window in which unique IDs stop being free: if the account exposes a stable id, switch to it **before** 1.0.0, because afterwards it is a migration.

After 1.0.0, prefer a migration path over a break.

### Migration shapes

**Unique ID migration** rewrites existing registry entries during setup, before the platforms load. The entity ID follows the registry entry, so the user's automations keep working. Migrations must be idempotent and must handle the case where they already ran.

```python
async def _async_migrate_unique_ids(hass: HomeAssistant, entry: {ClassPrefix}ConfigEntry) -> None:
    """Migrate entity unique IDs from the legacy format."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not entity.unique_id.startswith(f"{entry.entry_id}_"):
            continue
        new_unique_id = entity.unique_id.replace(entry.entry_id, account_id, 1)
        if registry.async_get_entity_id(entity.domain, DOMAIN, new_unique_id):
            # A collision means the new entity already exists — drop the stale one.
            registry.async_remove(entity.entity_id)
            continue
        registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
```

**Deprecation instead of removal**, when you cannot migrate automatically: raise a repair issue with `async_create_issue`, keep the deprecated path working for at least one release cycle, delete the issue with `async_delete_issue()` as soon as the condition no longer holds — a stale repair notification trains users to ignore them — and add the `issues.<issue_id>.title` / `.description` strings.

### Removing entities and devices

Removing an entity does not remove its registry entry, and **the entity registry writes an `unavailable` state for every registered entity that no longer has an entity object behind it** — so the entity lingers in the UI forever. Clean up explicitly with `er.async_get(hass).async_remove(entity_id)` during setup for the IDs you know are gone. The registry cascades downward: config entry → device → entity.

Entity names and entity IDs are generated from the **backend language at the moment the entity is created**, not the user's current UI language. Fixing wording in `en.json` therefore does not rename anyone's existing entities.

### Do not

- Do not rename something because the new name is nicer.
- Do not remove a config option because it looks unused.
- Do not batch a breaking change into an unrelated commit.
- Do not describe a change as non-breaking because the code still runs; the test is whether a user's existing setup still behaves the same.

## Modern APIs, and what not to copy from older integrations

Home Assistant changes fast enough that any pattern remembered from training data or copied from a blog post is suspect. The most common failure mode is confidently writing a 2023 API.

### Check the source, do not recall

The installed Home Assistant in `.venv` is the authority.

```sh
.venv/bin/python -c "from homeassistant.const import __version__; print(__version__)"
rg -n "def async_get_device_by_identifier" .venv/lib/python*/site-packages/homeassistant/helpers/device_registry.py
rg -n "deprecated|breaks_in_ha_version" .venv/lib/python*/site-packages/homeassistant/helpers/update_coordinator.py
rg -n "runtime_data" .venv/lib/python*/site-packages/homeassistant/components/<similar_integration>/
```

`breaks_in_ha_version=` in a `@deprecated_function` decorator tells you the actual removal deadline. Prefer that over any secondary source. When the installed source is not enough — a pattern that is new rather than deprecated — check <https://developers.home-assistant.io/blog/> before implementing.

Only a **primary** source settles a question: the installed source you import, or the current documentation of the tool you are configuring. A GitHub issue, a blog post and your own recall are leads, never answers. Two traps make a secondary source feel primary:

- **Silent failure.** Most configuration rejects nothing: an unknown key is ignored, and the resulting default often looks like success. Before believing "X works", ask what a broken X would look like — if the answer is "the same thing", the observation proved nothing. Test the case that can fail.
- **A key that belongs to a neighbouring tool.** That a key works in one tool is no evidence for the next, and the wrong one will not complain.

### The three that matter most here

1. **Device registry single ownership (2026.8).** Scope every lookup by `entry.entry_id`; the unscoped `async_get_device()` is out.
2. **Typed `runtime_data`.** State belongs on the entry, not in `hass.data`. `creating_component_code_review.md` upstream still recommends `hass.data[DOMAIN]`; that page is out of date and is contradicted by the Bronze `runtime-data` rule. Do not "correct" `entry.runtime_data` back to it.
3. **Warnings are errors in tests**, so a `DeprecationWarning` fails the suite. Fix the deprecation.

### Patterns an older integration will teach you wrongly

- `hass.data[DOMAIN]` instead of `entry.runtime_data`
- `FlowResult`, `DEVICE_CLASS_*` constants, `async_timeout`
- `device_trigger.py` / `device_condition.py` / `device_action.py`
- Creating an `aiohttp.ClientSession` instead of `async_get_clientsession(hass)`
- `from __future__ import annotations` — Python 3.14 evaluates annotations lazily already
- `[%key:…%]` references, which only work in Core (`strings.json` is fine here, mirrored by `strings:check`)
- A hand-written retry loop around `ConfigEntryNotReady`
- Adding a warning filter to make a deprecation go away

### Fixing a deprecation warning

1. Read the warning — it names the symbol, and usually the replacement and the removal version.
2. Confirm the replacement's real signature in the installed source. Signatures shift between the announcement and the release.
3. Replace every occurrence, including tests and diagnostics. A half-migrated codebase is worse than an un-migrated one.
4. `mise run check` and `mise run test`, then restart Home Assistant and confirm the warning is gone from the log.

## Python style

**4 spaces, 88 columns** (ruff's `line-length` in `pyproject.toml`), double quotes, full type hints, async for all I/O. YAML 2 spaces; JSON 2 spaces, no trailing commas, no comments.

### Typing

This repository runs basedpyright strict. Home Assistant declares entity attributes such as `device_info`, `native_value`, and `extra_state_attributes` as both class-level `_attr_*` values and properties, and strict mode reports every property override on an integration entity as `reportIncompatibleVariableOverride`. Suppress it per line on the override (`# pyright: ignore[reportIncompatibleVariableOverride]`); there is no project-wide setting that keeps the rest of strict mode.

- Annotate every parameter and return value. `-> None` on procedures. No bare `Any` where a `TypedDict` or dataclass is meant.
- Never `from __future__ import annotations`.
- `collections.abc` for abstract base classes, `typing` for `Any` and `TYPE_CHECKING`.
- Narrowing for the type checker goes **inside** a `TYPE_CHECKING` block, so it changes nothing at runtime:

  ```python
  if TYPE_CHECKING:
      assert self.config_entry is not None
  ```

- Docstrings are Google style when they need more than a summary line — `Args:`, `Returns:`, `Raises:`. Leave the types out; the annotations already carry them.

### Async

- `asyncio.gather()` for concurrent operations, `asyncio.timeout()` for timeouts. Never `time.sleep()`, a synchronous HTTP library, or any blocking call in the event loop — file and directory operations, `urllib` and SSL context loading all block, and so does calling an `async_*` API from a worker thread, which raises outright.
- `await hass.async_add_executor_job(fn, arg)` runs blocking I/O in an executor thread.
- **Background tasks belong to the config entry, not to `hass`** — the entry cancels them on unload, which `hass.async_create_task` does not. `entry.async_create_task` for work that must finish before unload; `entry.async_create_background_task` for long-lived loops. All of them default to `eager_start=True`: the coroutine runs synchronously up to its first `await` before the call returns, so ordering-sensitive code and tests will surprise you.
- `@callback` from `homeassistant.core` for event-loop functions that do no I/O. A missing decorator causes execution in an executor thread, which is the wrong context.
- Module-level imports are safe; anything conditional needs an import helper, because CPython's import machinery is not thread-safe. Type-only imports go in `if TYPE_CHECKING:`.

### Errors and logging

- `ServiceValidationError` when the user got something wrong (the stack trace is only logged at debug level, so they see a message rather than a wall of text); `HomeAssistantError` when the service failed (the full trace **is** logged). Both take `translation_domain`, `translation_key` and `translation_placeholders`, never a plain English string. **Never `ValueError`** — it is what these two exist to replace, and it reaches the user as an unhandled crash.
- `raise ... from err` to preserve the chain. Never a bare `except:`.
- `_LOGGER.exception()` inside an exception handler; `_LOGGER.info()` sparingly and only for something the user needs. No period at the end (syslog style), `%` formatting rather than an f-string, and never a credential, token, account number or meter number in any line at any level.
- Properties do no I/O, raise nothing, and have no side effects.

### Constants

Prefer `homeassistant.const` (`CONF_USERNAME`, `CONF_PASSWORD`, `UnitOfEnergy`, `CURRENCY_DOLLAR`) over defining new ones; only add to `const.py` what is used widely inside the integration. Construct a compound unit if no combined constant exists.

### Suppressions

`# noqa: CODE` and `# type: ignore[code]` are allowed where genuinely warranted — a false positive or an untyped external library — never to silence a real finding. Always with the specific code and a reason. Never bare `# noqa`, `# type: ignore` or `# ruff: noqa`.

## Comments

A comment is a claim that the reader needs something the code cannot give them. Usually that claim is false, and an unnecessary comment is not free: it has to be kept true, it goes stale without failing anything, and it tells the next reader "this line is unusual" when it is not. **The default is no comment.**

Write one only if it passes all five gates:

1. **Neighbourhood** — do the sibling entries at the same nesting level carry comments? Match the file you are in, not your own habit.
2. **Not a restatement** — a comment says _why_, never _what_.
3. **Not lookupable** — could the reader find this in seconds in the docs for a tool they are already using? Having had to research it once is not a reason to persist it.
4. **Still true in a year** — "currently the newest version", "fixed in the next release" rot silently and mislead precisely when someone finally reads them.
5. **Two lines at most** — anything longer is documentation.

What does earn one: a workaround for an external bug or limit, **with the issue URL**; a deliberate deviation from the approach a reader would expect; an ordering, timing or lifecycle constraint not visible from the lines involved; the provenance of a value that cannot become a named constant — a vendor quirk in the SmartHub payload is the case here; a security restriction that looks over-cautious out of context; and every `# noqa` / `# type: ignore` reason.

What never earns one: restating the code, banner separators, commented-out code, change narration ("changed from X", "added for issue #12"), general knowledge about Python or Home Assistant, anything the `EntityDescription` or a translation key already declares, a `TODO` with no tracked issue behind it, and **agent reasoning traces** — alternatives you rejected, what you tried first, why you looked something up. That belongs in the answer to the developer, not in their repository.

Docstrings in Python are structure, not commentary: module, class and public function, and Ruff enforces them.

## Validation

```sh
mise run check   # markdown, ruff format, ruff lint, basedpyright
mise run fix     # every formatter and autofixer
mise run test    # pytest
```

Run `mise run fix`, then `mise run check` until it exits 0. Type findings are never auto-fixed, so those are always a manual loop.

`hassfest`, Home Assistant's own validator for `manifest.json`, translations and integration structure, is the gate that catches a missing translation key before a user does. It is not wired into this repository yet; see DEV.md.

**When a fix does not take:** try once more with a different approach, and if that fails too, stop and explain what you tried rather than looping. Report failing terminal commands, network timeouts and failed git operations instead of working around them. After three failed attempts at the same error, stop and report what you tried and what you observed — a wrong mental model does not improve with repetition.

### The local Home Assistant instance

Running Home Assistant against the checked-out integration, and the tooling that reads its state, are not decided yet; see DEV.md. Until they are, the rules that already hold: restart after **any** change to Python files, `manifest.json`, translations or the config flow; read the **first** error in a cascade, not the last; and a config flow missing after a restart is usually the frontend cache, not the code — hard-refresh the browser before debugging the flow.

The production instance is an appliance reached through the `home-assistant` MCP server. Treat it as read-only for smoke checks.

### Localising a runtime failure

| Symptom                                           | Layer to inspect                                                           |
| ------------------------------------------------- | -------------------------------------------------------------------------- |
| Integration missing from the add-integration list | `manifest.json`, or an import error at module load — check the startup log |
| "Config entry not ready, retrying"                | `_async_update_data` / `_async_setup` raising `ConfigEntryNotReady`        |
| Entry loads, all entities `unavailable`           | the coordinator's first refresh failed, or `last_update_success` is false  |
| Entities available but values `unknown`/`None`    | key mismatch between `coordinator.data` and the entity's read path         |
| Values never change                               | `update_interval`, caching in the API client, or a swallowed error         |
| Endless reauth prompts                            | `ConfigEntryAuthFailed` raised for a non-auth failure                      |
| "Detected blocking call inside the event loop"    | sync I/O in async code                                                     |
| Entity duplicated after an update                 | `unique_id` changed — a breaking change                                    |

A runtime bug that reached a user is exactly the case where a test pays for itself. Add one that reproduces the original failure before the fix.

## Working with the developer

### When instructions conflict with a request

Say which instruction the request contradicts and restate what you understood, then follow the developer's decision. If it reflects a permanent change of approach, offer to update this file — and propose updates whenever you notice repeated deviations, stale rules, or a new pattern worth standardising.

### Never post to Open Home Foundation repositories

`home-assistant/core`, the developer docs, the brands repo. Their AI policy closes anything it believes an agent filed, so draft it locally and hand it over.

## Reference

- [Integration Quality Scale](https://developers.home-assistant.io/docs/integration_quality_scale_index)
- [Fetching Data](https://developers.home-assistant.io/docs/integration_fetching_data)
- [Integration Setup Failures](https://developers.home-assistant.io/docs/integration_setup_failures)
- [Config Entries](https://developers.home-assistant.io/docs/config_entries_index)
- [Entity Developer Docs](https://developers.home-assistant.io/docs/core/entity)
- [Custom Integration Localization](https://developers.home-assistant.io/docs/internationalization/custom_integration)
- [Home Assistant Core's own tests](https://github.com/home-assistant/core/tree/dev/tests/components) — the reference for a pattern this file does not cover

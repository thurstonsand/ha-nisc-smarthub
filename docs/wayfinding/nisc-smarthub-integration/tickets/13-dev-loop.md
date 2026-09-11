---
status: closed
type: grilling
claimed: 2b
blocked-by: [7]
---

# Development loop, tests, CI, and packaging

## Question

How the integration is developed, tested, released, and installed, given the scaffold already in place.

Branches to settle, against [Home Assistant custom integration practice](07-ha-integration-practice.md):

- The local instance: a `mise run dev` task that runs `hass --config config` with `homeassistant==2026.9.1` in the uv dev group and `custom_components/` reachable; what under `config/` is tracked (a minimal `configuration.yaml`?) versus ignored; how the agent reads its state without a browser (the `hass` REST/WS API with a locally minted token, or a small script).
- Unit tests with `pytest-homeassistant-custom-component` pinned to the HA version: what is mocked (the SmartHub client, from recorded fixtures of real responses with identifiers scrubbed), what is exercised for real (statistics writes into the test recorder).
- Fixture capture: how real poll responses become test fixtures without leaking account, meter, or address.
- CI: whether `mise run check` and `mise run test` on ubuntu-latest suffice, and whether hassfest / HACS validation actions are added.
- Release: version in `manifest.json`, git tags, GitHub Releases, `hacs.json` fields; whether release-please or a manual bump task.
- The appliance smoke path via MCP: add custom repo, download, restart, drive config flow, verify statistics; kept as a runbook for the next effort.

Cost of being wrong: low; all of this is revisable. The one sticky choice is fixture format, since tests accumulate against it.

## Resolution

Grilled in one round on 2026-09-11 against [ha-integration-practice](../research/ha-integration-practice.md), with two follow-up facts: the canonical [ludeeus/integration_blueprint](https://github.com/ludeeus/integration_blueprint) tracks `config/configuration.yaml` (`default_config:`, `homeassistant: debug: true`, a `logger:` block) and ignores the rest of `config/`; and Amp orbs are Debian 12 machines with `uv` preinstalled, prepared by a committed `.agents/setup` that Amp snapshots, with long-running servers declared in `.amp/services.yaml` and wake-time work in `.agents/resume` ([docs](https://ampcode.com/docs/orbs/customizing)). Thurston's rule for this ticket: do what is idiomatic for HA and HACS.

**Local instance.** `config/configuration.yaml` tracked, blueprint-shaped (`default_config`, debug, logger with `custom_components.nisc_smarthub: debug`, `http: server_host: 127.0.0.1`); everything else under `config/` ignored. `mise run dev`: `ensure_config` when `config/` lacks the generated files, symlink `config/custom_components -> ../custom_components` when absent and fail on anything else at that path, then `uv run --no-sync hass --config "$PWD/config" --debug` in the foreground (agents wrap it in tuistory).

**Dependencies.** The dev group pins `homeassistant==2026.9.1` and `pytest-homeassistant-custom-component==0.13.364` (Python >= 3.14.2). For now `hass` installs the runtime closure (frontend, `default_config` requirements) at first start; no `--skip-pip`. Declaring that closure in the dev group and switching to `--skip-pip` is the first follow-up once the dev config settles, and orbs make it worth doing early: `.agents/setup` runs `mise install` and `uv sync --dev`, and a snapshot without the closure pays a pip install on every fresh orb.

**Orbs.** Supported from the start: `.agents/setup` (mise, `uv sync --dev`, idempotent), `.amp/services.yaml` declaring the dev instance on 8123 as a portal, `.agents/resume` running `dev:bootstrap` when `config/.agent-token` is absent. Fixture capture needs 1Password and stays a laptop task. Whether a portal reaches a loopback-bound server is unverified; the build checks it and switches `server_host` if it must.

**Agent access.** `mise run dev:bootstrap` performs headless onboarding on a fresh instance (`POST /api/onboarding/users` for an auth code, exchange at `/auth/token`, mint a long-lived token over WebSocket) and writes `config/.agent-token`. Internal API; breakage on HA upgrade is expected maintenance and fails loudly. Inspection is REST and the recorder WebSocket commands.

**Tests.** `tests/` at the root, `conftest.py` with `enable_custom_integrations`, `asyncio_mode = auto`. Pure tests for the tariff and client parsing; `MockConfigEntry` integration tests with the client mocked at its boundary; the real recorder for statistics; Syrupy snapshots for states and diagnostics. Coverage gate 95% on `custom_components/` via pytest-cov `fail-under`, in `mise run test` and CI.

**Fixtures.** `scripts/capture_fixtures.py` logs in through `op`, pulls `user-data`, `billing`, `service-locations/<loc>`, and hourly/daily polls for a fixed range, applies a deterministic scrub map (account, location, meter including inside series ids, email, name, address, hint, coordinates, tax ids; token dropped), fails if any real value survives, and writes `tests/fixtures/*.json`, which are committed.

**Lint.** Ruff harvests Core's 2026.9.1 selection and deliberate ignores, dropping only monorepo-specific rules; the current subset stays where it is additive. Line length 88. `strings.json` is authored and `translations/en.json` shipped; a mise task checks they agree and joins `check`.

**CI.** `mise run bootstrap`, `check`, `test`, plus hassfest (`home-assistant/actions/hassfest`) and HACS validation (`hacs/action`, category integration).

**Release.** `mise run release <version>` bumps `manifest.json`, commits, tags `v<version>`, `gh release create` (pre-release flag for pre-releases). `hacs.json` is `{"name": "NISC SmartHub", "homeassistant": "2026.9.0"}`; no `zip_release`.

**Client.** Bundled at `custom_components/nisc_smarthub/smarthub/`, no HA imports, aiohttp session injected. Extracted to PyPI only if a second consumer appears.

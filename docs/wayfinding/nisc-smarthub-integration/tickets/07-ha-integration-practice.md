---
status: closed
type: research
claimed: subagent
blocked-by: []
---

# Home Assistant custom integration practice

## Question

What Home Assistant itself prescribes for a cloud-polling custom integration in 2026, so the design and the development loop follow Core's opinions rather than inventing our own.

Pin against the developer docs (developers.home-assistant.io), the Integration Quality Scale, `home-assistant/core` at 2026.9.x, and the tooling projects' own READMEs:

- Style and structure: the developer docs' style guide, the expected module layout (`__init__.py`, `config_flow.py`, `coordinator.py`, `sensor.py`, `const.py`, `manifest.json`, `strings.json` + `translations/`), `DataUpdateCoordinator` conventions, `ConfigEntry` runtime data, `EntityDescription` + `translation_key`, unique-id rules, device registry ownership (2026.8 changes).
- Quality Scale rules that apply to a statistics-writing, cloud-polling integration with a config flow: which are required for Bronze/Silver, and which are irrelevant here.
- Config flow: reauth and reconfigure flows, options flow, `data` vs `options`, entry `VERSION`/`MINOR_VERSION` and `async_migrate_entry`.
- `manifest.json` requirements for a HACS custom integration (`version`, `requirements`, `dependencies: ["recorder"]`, `iot_class`), and `hacs.json`.
- Local development loop for a custom integration outside a devcontainer: installing `homeassistant==2026.9.1` into a venv, running `hass --config <dir>` with `custom_components/` symlinked or pointed at, the `-c`/`--skip-pip` flags that matter, and how `pytest-homeassistant-custom-component` is versioned against Home Assistant.
- Testing conventions from Core's `AGENTS.md`: fixtures, snapshot testing with syrupy, `MockConfigEntry`, parametrize over branching.
- Release and distribution through HACS custom repositories: tagging, `zip_release`, what HACS reads from `hacs.json` and GitHub releases.

Output: a reference that the harvest ticket and the development-loop ticket can both cite, with explicit "Core says" attributions.

## Resolution

[Research](../research/ha-integration-practice.md) distinguishes Core's rules from our choices, recommends applicable Bronze/Silver requirements plus selected higher-tier practices, and uses Platinum cloud-polling Opower at 2026.9.1 as the structural example. The local recipe pins `homeassistant==2026.9.1` with PyPI-verified `pytest-homeassistant-custom-component==0.13.364`, requires Python >=3.14.2, links the checkout into ignored `config/custom_components`, launches through mise and uv, and documents authenticated browserless inspection plus the limits of untested headless onboarding and `--skip-pip`.

---
status: closed
type: task
claimed: subagent
blocked-by: []
---

# Harvest HA_DEV.md

## Question

Produce `HA_DEV.md` at the repo root from the agent material in [`jpawlowski/hacs.integration_blueprint`](https://github.com/jpawlowski/hacs.integration_blueprint): its `AGENTS.md`, `.agents/instructions/*.md`, and the skills that apply. This is a task, not a decision: the map already chose to keep those rules in their source's voice.

Rules for the harvest:

- Keep what applies to *this* integration: a cloud-polling, config-flow-only integration with a coordinator, sensor entities, external statistics, diagnostics, translations, and tests. Drop what does not: device platforms we will never have (switch, fan, number, select, button), device triggers, repairs UI, subentries, template-sync machinery, devcontainer and Copilot workflow, `script/*` wrappers (we use mise), and the "which repository is this" preamble.
- Copy-paste as much as is reasonable; small edits to make cut sections flow are fine. Do not rewrite into a different voice.
- Fix references so they point at real things here: mise tasks instead of `script/lint`; no `.agents/instructions/` routing table unless the files are actually brought over (decide: inline the relevant instruction content into `HA_DEV.md` sections rather than importing eighteen files).
- Cite the source repository and commit at the top.
- Do not touch `AGENTS.md`, `CONTEXT.md`, or `DEV.md`; `AGENTS.md` already links `HA_DEV.md`.

Resolved when `HA_DEV.md` exists, `mise run docs:lint` passes on it, and the resolution names what was kept, what was dropped, and anything in the source that looked wrong or outdated for HA 2026.9.

## Resolution

`HA_DEV.md` exists at the repo root: 856 lines, harvested from `jpawlowski/hacs.integration_blueprint` at commit `0df158948e717acac4fe19fc51f40e31bd090bae` (unchanged since the charting fetch on 2026-09-07). `mise exec -- markdownlint-cli2 HA_DEV.md` passes.

Seventeen sections, organized by topic rather than by source file: contracts that hold everywhere (with the 2026.8 device registry ownership rules), integration structure, coordinator and API client, config flow, entities, statistics/recorder/diagnostics, translations and icons, tests, breaking changes, modern APIs and what not to copy, Python style, comments, validation, working with the developer, reference.

### Kept

The source's `AGENTS.md` contracts list, device registry single-ownership rules, package layout, and Quality Scale posture. The whole of `blueprint.coordinator` (three-layer rule, client rules, exception hierarchy and its mapping table, update interval, first refresh, both caching modes) plus the four coordinator failures from `ha-coordinator-debug` that the mapping table cannot express, and its symptom-to-layer table. `blueprint.config_flow` minus discovery and subentries. `blueprint.entities` minus every platform we will not have. `blueprint.diagnostics` verbatim, with the account number, service location id, and meter number added to the redaction list since this repo is public. `blueprint.translations` and the `ha-translations` key table, cut to the keys a sensor-only integration can need. `blueprint.tests` and `ha-testing` merged into one section, including the `conftest.py` bootstrap rewritten for `nisc_smarthub`. `ha-breaking-changes` nearly whole, because the pre-1.0.0 guidance is exactly where this integration sits. `ha-modern-apis` as verification discipline plus a list of patterns an older integration teaches wrongly. `blueprint.python`, `blueprint.comments`, the commit-message conventions, the scope-of-change rules, the "code that predates the current rules" bounds, and the developer-vocabulary warning, which is the single most transferable thing in the source.

### Dropped

The template-sync preamble and "which repository is this" block; the eighteen-file routing table and the instructions/skills loading mechanics; the Copilot / Claude Code / Codex frontmatter matrix and `blueprint.markdown`'s instructions-file contract; devcontainer setup and its CLI tool inventory; `.agents/scratch` and the leaving-a-task-unfinished protocol; the community AI policy and the whole posting-on-the-developer's-behalf section; `blueprint.repairs`, `blueprint.service_actions`, `blueprint.services_yaml`, `blueprint.configuration_yaml`, `blueprint.shell`, `blueprint.yaml`, `blueprint.json`, `blueprint.manifest` (kept only as the `config_flow.py` shim requirement and the `iot_class`/`integration_type` mention inside other sections); discovery flows, subentries, OAuth2 redirect flows, device triggers beyond the do-not-copy warning, and every write platform except as the `PARALLEL_UPDATES` rule that explains why `sensor` takes `0`. The `ha-quality-review` report template went too: it is a review procedure, not a rule, and the rules it enforces are already stated where they belong. The `custom_components/` ripgrep trap was dropped because it is caused by the source's own `.gitignore`, which this repo does not share.

### Adapted

`script/lint` and `script/type-check` became `mise run check` / `mise run fix`; `script/test` became `mise run test`; `script/develop`, `script/ha`, and the shared-instance etiquette became a short "see DEV.md" note keeping only the rules that survive any choice of loop (restart after a Python or manifest change, read the first error in a cascade, hard-refresh before debugging a missing flow). `script/hassfest` is named as the validator that catches missing translation keys and flagged as not yet wired up, also pointing at DEV.md. The `.venv` grep paths carried over unchanged since uv puts the interpreter in the same place.

### Wrong or outdated in the source

- **`UpdateFailed(retry_after=60)`** in `blueprint.coordinator`'s exception mapping table. `UpdateFailed` takes no `retry_after` argument in HA Core; it is `HomeAssistantError` with a message. Dropped along with the rate-limit row, which SmartHub has given no reason to want.
- **120-column Python.** This repo's ruff config sets 88, so the style section says 88. The source's four-space indent and double quotes agree with ours.
- **`pytest.mark.unit` / `pytest.mark.integration` markers.** This repo runs pytest with `--strict-markers` and registers neither, so copying the marker convention would have failed every test. Dropped rather than invented.
- **`freezer` attributed to `pytest-freezegun`.** The fixture reaches a HA custom-integration suite through `pytest-homeassistant-custom-component`, not that plugin. Kept the pattern, dropped the attribution.
- **`entity_registry_enabled_default` guidance is stated twice in the source** with slightly different emphasis in `blueprint.entities` and `ha-entity-platform`. Merged into one statement.
- Nothing in the source contradicts HA 2026.9 outright. Its device registry, `runtime_data`, and `from __future__ import annotations` rules are all written for 2026.8+ and Python 3.14, which is why it was worth harvesting.

One gap worth naming for the design doc: the source has nothing on external statistics or the recorder's import API, which is the center of this integration. The statistics section here covers only what the blueprint actually knows (state classes, units, UTC timestamps, what breaks history), and the reconcile-window semantics stay in `CONTEXT.md` and the design doc where they belong.

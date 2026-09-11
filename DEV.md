# DEV.md

## Setup

`mise trust` then enter the directory. The `enter` hook syncs the uv venv and installs hk's pre-commit hooks.

## Commands

```sh
mise run check             # every non-mutating check: markdown, ruff format, ruff lint, basedpyright
mise run fix               # every formatter and autofixer
mise run test              # pytest
mise tasks                 # the full list
```

## Layout

```text
custom_components/nisc_smarthub/   the integration (not yet created)
docs/wayfinding/                   the planning map for this effort
docs/designs/                      accepted design docs
tests/                             pytest suite
config/                            local Home Assistant dev instance (ignored)
```

The local development loop (running Home Assistant against the checked-out integration) is decided in `docs/designs/01-nisc-smarthub.md`, decision 14; its `mise run dev` and `dev:bootstrap` tasks land with Phase 2 and get documented here then.

## Home Assistant

The production instance is reachable through the `home-assistant` MCP server.

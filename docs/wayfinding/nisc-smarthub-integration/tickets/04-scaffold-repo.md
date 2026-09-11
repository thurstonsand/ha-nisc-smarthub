---
status: closed
type: task
claimed: 2b
blocked-by: []
---

# Scaffold the repository

## Question

Create `../ha-nisc-smarthub` as a public-ready repository in Thurston's conventions so the map and its research have a home and later tickets inherit the tooling: mise-managed environment, hk pre-commit, renovate, ruff + basedpyright + pytest, markdownlint, CI, and the `home-assistant` MCP config.

## Resolution

Created 2026-09-11, ported from ansiblonomicon and grand-trmnl:

- `mise.toml` (python 3.14, hk, markdownlint-cli2; `bootstrap` on enter; `check`, `fix`, `test`, and the per-tool tasks), `hk.pkl` (ruff check/format, markdownlint, basedpyright; pre-commit with `stash = "git"`), `renovate.json` (mise + hk + GitHub Actions grouping, digest pinning), `pyproject.toml` (ruff select list, basedpyright strict, pytest), `.markdownlint-cli2.yaml`, `.github/workflows/ci.yml` (`mise run check` and `mise run test`), MIT `LICENSE`.
- `.mcp.json` with the `home-assistant` OAuth server, tracked as ansiblonomicon (also public) tracks the same URL; the webhook is OAuth-protected. `config/` (local HA instance) is ignored pending the development-loop ticket.
- `AGENTS.md`, `CONTEXT.md`, `DEV.md`, `README.md`. `HA_DEV.md` is the harvest ticket's output.
- No integration code, no `custom_components/`, no `uv.lock` yet (first `mise` entry writes it).

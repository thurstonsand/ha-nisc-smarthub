# Release

`ha-nisc-smarthub` is installed through HACS as a custom repository, and HACS offers the GitHub releases of this repository. There is no release workflow and no changelog: a release is a `vX.Y.Z` tag with a GitHub release whose notes GitHub generates from the commits since the previous one.

HACS reads the installed version from `custom_components/nisc_smarthub/manifest.json` in the downloaded tag, so `manifest.json` on `main` carries the version of the newest release, and the tag and the manifest always agree. The `version` in `pyproject.toml` stays `0.0.0`, since HACS never reads it. `hacs.json`'s `homeassistant` is the oldest Home Assistant that HACS installs this release on.

Versions follow semantic versioning: a release with any new feature (an `Added` entry), a raised floor (a higher minimum Home Assistant, Python, or other dependency version), or anything breaking is at least minor, and breaking is major from 1.0 on; patch is only for fixes and dependency updates.

## Release flow

1. Push the release's changes to `main` and wait for CI to pass.
2. Run `mise run release X.Y.Z`, or `mise run release X.Y.Z --dry-run` to see what it would do. `scripts/release.py` refuses unless `X.Y.Z` is a semver version, the checkout is on `main` with a clean tree, and `vX.Y.Z` does not exist yet. Then it:
   - writes `X.Y.Z` into `manifest.json`;
   - commits `Release vX.Y.Z`;
   - creates the annotated tag `vX.Y.Z`;
   - pushes `main` and the tag together;
   - creates the GitHub release with `gh release create --generate-notes`.
3. A version with a pre-release component, such as `0.4.0-rc.1`, is published as a GitHub pre-release, which HACS offers only to users who set the repository to show beta versions.

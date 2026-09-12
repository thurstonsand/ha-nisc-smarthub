"""The manifest and the integration's constants must agree."""

import json
from pathlib import Path
from typing import Any

from custom_components.nisc_smarthub.const import DOMAIN

MANIFEST = Path("custom_components") / DOMAIN / "manifest.json"


def manifest() -> dict[str, Any]:
    """Load the integration manifest."""
    return json.loads(MANIFEST.read_text())


def test_manifest_declares_the_domain() -> None:
    """The manifest's domain is the directory name and the constant."""
    assert manifest()["domain"] == DOMAIN
    assert MANIFEST.parent.name == DOMAIN


def test_manifest_pins_its_requirements() -> None:
    """HACS and Core both want exact requirement pins."""
    assert all("==" in requirement for requirement in manifest()["requirements"])


def test_hacs_manifest_names_the_integration() -> None:
    """HACS needs a name and a minimum Home Assistant version."""
    hacs = json.loads(Path("hacs.json").read_text())
    assert hacs == {"name": "NISC SmartHub", "homeassistant": "2026.9.0"}

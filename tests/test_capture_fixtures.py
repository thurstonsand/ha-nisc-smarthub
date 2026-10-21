"""The fixture scrub, rerun over fixtures it already scrubbed."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = Path(__file__).parent.parent / "scripts" / "capture_fixtures.py"


def capture() -> ModuleType:
    """Load the capture script, which is a script and not a package."""
    spec = importlib.util.spec_from_file_location("capture_fixtures", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rescrubbing_the_fixtures_leaves_them_as_they_are() -> None:
    """`--from-fixtures` is how a policy change is proved, so it has to be a no-op.

    The recorded customer name is a placeholder, and a placeholder read back
    must not be harvested as a member's value and then reported as a leak.
    """
    script = capture()
    recorded = {
        path.stem: json.loads(path.read_text())
        for path in sorted(FIXTURES.glob("*.json"))
    }

    scrubbed = script.scrub_all(recorded, script.Tokens())

    assert scrubbed == recorded

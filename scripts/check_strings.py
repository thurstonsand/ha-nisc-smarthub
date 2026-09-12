"""Check that the authored strings and the shipped English translation agree.

Home Assistant compiles `strings.json` into `translations/en.json` for its own
integrations. A custom integration has no such build step: the file it ships is
served exactly as written, so the two must be kept identical by hand, and a
`[%key:...%]` reference would reach the user verbatim.
"""

import json
from pathlib import Path
import sys
from typing import Any, Final, cast

INTEGRATION: Final = Path("custom_components") / "nisc_smarthub"
STRINGS: Final = INTEGRATION / "strings.json"
ENGLISH: Final = INTEGRATION / "translations" / "en.json"


def flatten(payload: Any, path: str = "") -> dict[str, str]:
    """Return every leaf of a translation file keyed by its dotted path."""
    if not isinstance(payload, dict):
        return {path: str(payload)}
    branches = cast("dict[str, Any]", payload)
    return {
        key: value
        for name, child in branches.items()
        for key, value in flatten(child, f"{path}.{name}" if path else name).items()
    }


def main() -> int:
    """Compare the two files and report every disagreement."""
    authored = flatten(json.loads(STRINGS.read_text()))
    shipped = flatten(json.loads(ENGLISH.read_text()))

    problems = [
        f"{key}: only in {STRINGS}" for key in sorted(set(authored) - set(shipped))
    ]
    problems += [
        f"{key}: only in {ENGLISH}" for key in sorted(set(shipped) - set(authored))
    ]
    problems += [
        f"{key}: differs between the two files"
        for key in sorted(set(authored) & set(shipped))
        if authored[key] != shipped[key]
    ]
    problems += [
        f"{key}: uses a [%key:...%] reference, which only Home Assistant Core resolves"
        for key, value in sorted(shipped.items())
        if value.startswith("[%key:")
    ]

    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(f"{len(authored)} strings agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Every tariff the integration ships has the words and fields its declarations need."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from custom_components.nisc_smarthub.config_flow import form_key
from custom_components.nisc_smarthub.const import DOMAIN
from custom_components.nisc_smarthub.tariff import EFFECTIVE_FROM, TARIFFS, Tariff

INTEGRATION = Path("custom_components") / DOMAIN
RATE_STEPS = ("cycle", "add_rate_version")


def strings() -> dict[str, Any]:
    """Load the authored strings."""
    return json.loads((INTEGRATION / "strings.json").read_text())


@pytest.mark.parametrize("tariff", TARIFFS.values(), ids=list(TARIFFS))
def test_every_rate_field_is_labelled_and_described(tariff: Tariff) -> None:
    """A field without strings renders as its raw key on the form."""
    steps = strings()["config"]["step"]
    names = [EFFECTIVE_FROM, *(parameter.key for parameter in tariff.rate_parameters)]

    unlabelled = [
        (step, part, form_key(tariff, name))
        for step in RATE_STEPS
        for part in ("data", "data_description")
        for name in names
        if form_key(tariff, name) not in steps[step][part]
    ]

    assert unlabelled == []


@pytest.mark.parametrize("tariff", TARIFFS.values(), ids=list(TARIFFS))
def test_every_bill_line_is_a_finalize_field(tariff: Tariff) -> None:
    """The action's form can only offer the lines services.yaml declares."""
    declared = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    fields = declared["finalize_cycle"]["fields"]
    described = strings()["services"]["finalize_cycle"]["fields"]

    missing = [
        parameter.key
        for parameter in tariff.bill_parameters
        if parameter.key not in fields or parameter.key not in described
    ]

    assert missing == []

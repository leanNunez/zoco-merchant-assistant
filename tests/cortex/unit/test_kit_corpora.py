"""The documents of each company are given to the harness, not decided by it."""

from pathlib import Path

from cortex.bootstrap import load_harness_data
from cortex.kit_adapter.corpora import kit_corpora, load_kit_data

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit"


def test_the_kit_gives_its_documents_to_the_first_company_only() -> None:
    assert dict(kit_corpora(KIT)) == {"comercio_a": ROOT / "docs"}


def test_the_harness_data_takes_the_corpora_it_is_given() -> None:
    nobody = load_harness_data(KIT, {})
    kit = load_kit_data(KIT)

    assert nobody.docs is not kit.docs
    assert nobody.config == kit.config
    assert nobody.catalog.names == kit.catalog.names

"""Which documents each company of the kit may search.

The kit's companies are fixtures, so the mapping lives here and not in the composition root:
`load_harness_data` is given the corpora explicitly and knows no company by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from cortex.bootstrap import HarnessData, load_harness_data


def kit_corpora(kit_dir: Path) -> Mapping[str, Path]:
    """Comercio A keeps the documents of the first delivery; Comercio B has none."""
    return MappingProxyType({"comercio_a": kit_dir.parent / "docs"})


def load_kit_data(kit_dir: Path) -> HarnessData:
    """The kit as delivered: its fixtures and the documents of its first company."""
    return load_harness_data(kit_dir, kit_corpora(kit_dir))

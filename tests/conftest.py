"""Shared pytest setup."""

from pathlib import Path

import pytest
from hypothesis import settings

from cortex.bootstrap import HarnessData, load_harness_data

# Property tests must be reproducible: a failure in CI has to fail again on a laptop.
settings.register_profile("deterministic", derandomize=True, max_examples=200, deadline=None)
settings.load_profile("deterministic")


# --------------------------------------------------------------------------- the harness (cortex)

KIT = Path(__file__).resolve().parents[1] / "kit"


@pytest.fixture(scope="session")
def harness_data() -> HarnessData:
    """The kit as delivered, loaded and checked once for the whole session."""
    return load_harness_data(KIT)

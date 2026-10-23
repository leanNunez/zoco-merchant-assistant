"""Shared pytest setup."""

from hypothesis import settings

# Property tests must be reproducible: a failure in CI has to fail again on a laptop.
settings.register_profile("deterministic", derandomize=True, max_examples=200, deadline=None)
settings.load_profile("deterministic")

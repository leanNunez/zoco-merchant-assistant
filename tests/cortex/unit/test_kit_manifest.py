"""The evaluation kit is what IT asked for: it must stay byte for byte as delivered."""

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit"
MANIFEST = KIT / "MANIFEST.sha256"


def _entries() -> dict[str, str]:
    entries = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        entries[name] = digest
    return entries


def _location(name: str) -> Path:
    # The public scenarios live at the repository root, where the contract's command expects them.
    return ROOT / name if name.startswith("examples/") else KIT / name


@pytest.mark.parametrize("name", sorted(_entries()))
def test_kit_file_is_unmodified(name: str) -> None:
    actual = hashlib.sha256(_location(name).read_bytes()).hexdigest()

    assert actual == _entries()[name], f"{name} differs from the kit IT delivered"


def test_no_file_was_added_to_the_kit() -> None:
    present = {
        path.relative_to(ROOT).as_posix().removeprefix("kit/")
        for folder in (KIT, ROOT / "examples")
        for path in folder.rglob("*")
        if path.is_file() and path != MANIFEST
    }

    assert present == set(_entries())

"""The committed data/reclamos.db is an empty template with the adapter's schema."""

import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
DATABASE = ROOT / "data" / "reclamos.db"
SCRIPT = ROOT / "scripts" / "init_reclamos_db.py"


def open_read_only(path: Path) -> sqlite3.Connection:
    # Read-only so that checking the committed file can never modify it.
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def test_the_committed_database_is_valid_and_has_no_complaints() -> None:
    with closing(open_read_only(DATABASE)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("SELECT COUNT(*) FROM complaints").fetchone() == (0,)


def test_the_committed_database_has_the_expected_schema() -> None:
    with closing(open_read_only(DATABASE)) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        # (cid, name, type, notnull, default, pk)
        columns = connection.execute("PRAGMA table_info(complaints)").fetchall()
        unique_indexes = [
            row
            for row in connection.execute("PRAGMA index_list(complaints)").fetchall()
            if row[2] == 1  # the `unique` flag
        ]
        indexed = [
            connection.execute(f"PRAGMA index_info({row[1]})").fetchall() for row in unique_indexes
        ]

    assert tables == [("complaints",), ("sqlite_sequence",)]
    assert columns == [
        (0, "seq", "INTEGER", 0, None, 1),
        (1, "idempotency_key", "TEXT", 1, None, 0),
        (2, "conversation_id", "TEXT", 1, None, 0),
        (3, "reason", "TEXT", 1, None, 0),
        (4, "description", "TEXT", 1, None, 0),
        (5, "created_at", "TEXT", 1, None, 0),
    ]
    assert [[column[2] for column in index] for index in indexed] == [["idempotency_key"]]


def test_the_script_recreates_the_same_bytes_on_every_run(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("init_reclamos_db", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "reclamos.db"

    module.main(["init", str(target)])
    first = target.read_bytes()
    module.main(["init", str(target)])  # removes what was there before

    assert target.read_bytes() == first

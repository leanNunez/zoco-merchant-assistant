"""Recreate data/reclamos.db: the EMPTY initial claims database, built from the adapter's schema.

It is a template. The application writes to `COMPLAINTS_DB_PATH` (`var/reclamos.db` by
default, created on first use and git-ignored), so the committed file never accumulates
test complaints. The old file is removed first so the result does not depend on what was
there before.
"""

from __future__ import annotations

import sys
from pathlib import Path

from merchant_assistant.complaints.adapters.sqlite_repository import SqliteComplaintRepository

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "reclamos.db"


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else OUTPUT
    target.unlink(missing_ok=True)
    SqliteComplaintRepository(target).initialize()
    sys.stdout.write(f"wrote {target}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Where the console keeps what it never prints: tracebacks and warnings.

The log file takes every WARNING and above from the whole process (the console, the agent, the
SDK), so nothing technical reaches the terminal. It can contain exception text, so it is created
readable by its owner only and it lives under `var/`, which is git-ignored.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_LOG_PATH = Path("var/chat.log")
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


@contextmanager
def file_logging(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()

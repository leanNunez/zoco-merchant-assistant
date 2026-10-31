"""The closed alphabets and the length caps of every identifier the harness accepts.

Identifiers end up in SQL parameters, file names and traces, so each kind has a small alphabet
and a cap, and they live here, in one place, instead of beside each user:

- IDENTIFIER: companies and principals (lower case, from the server's directory).
- EXTERNAL_ID: conversations and runs (they come from Zoco Chat or the caller).
- OPERATION_ID_PATTERN: the idempotency key of a write, a pattern for the tool's schema.
- PLAIN_TOOL_NAME: a tool name the model chose, shown only when it is a plain name.

The caps are named so that a length is never a bare number inside a pattern.
"""

from __future__ import annotations

import re

MAX_IDENTIFIER_CHARS = 64
MAX_EXTERNAL_ID_CHARS = 128
MAX_OPERATION_ID_CHARS = 64
MAX_TOOL_NAME_CHARS = 64

# `{0,n}` after the first character: the whole id may have `n + 1` characters.
IDENTIFIER = re.compile(rf"[a-z0-9][a-z0-9_]{{0,{MAX_IDENTIFIER_CHARS - 1}}}", re.ASCII)
EXTERNAL_ID = re.compile(rf"[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_EXTERNAL_ID_CHARS - 1}}}", re.ASCII)
OPERATION_ID_PATTERN = rf"^[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_OPERATION_ID_CHARS - 1}}}$"
PLAIN_TOOL_NAME = re.compile(rf"[A-Za-z_][A-Za-z0-9_]{{0,{MAX_TOOL_NAME_CHARS - 1}}}", re.ASCII)

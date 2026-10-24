"""Whether a user message is a yes, a no, or anything else.

This classifier is the only thing that can turn a pending complaint into a confirmation, so
it is deliberately conservative. A message is AFFIRMATIVE only when it is one short line
made entirely of confirmation words and at least one of them is a real "yes"; NEGATIVE only
when it is made entirely of refusal words and one of them is a real "no". Everything else,
including a yes mixed with a no ("si no"), a yes with a change ("si pero cambia el motivo")
a yes followed by more instructions on another line, or a question ("¿sí?"), is OTHER.

The trade-off is a rigid experience, softened on purpose: the vocabulary includes NEUTRAL
words (articles, "mi", "reclamo", "registra", "eso", "gracias"...) that cannot change what is
being accepted, so "sí, confirmo el reclamo" or "dale, registra el reclamo" count. A word that
could change it ("pero", "otro", "cambia", "motivo", "no") is not in the vocabulary, so any
message that uses one is OTHER: the pending action is dropped and the agent has to ask again.
That is the safe failure. The unsafe one, registering a complaint the user did not clearly
accept, is what this module exists to prevent.

Known idioms that now read as a yes although they hedge: "si eso" and "si bien" said alone.
They are rare as a whole message, and the confirmation still has to name the exact values.
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum

MAX_WORDS = 6

AFFIRMATIVE_WORDS = frozenset(
    {
        "si", "confirmo", "confirmado", "dale", "ok", "okay", "listo", "correcto", "perfecto",
        "adelante", "registralo", "registrar", "de", "acuerdo", "por", "favor", "claro", "exacto",
        # Neutral: they cannot change WHAT is accepted, only how politely or fully it is said.
        "el", "la", "lo", "mi", "reclamo", "reclamos", "registra", "registre", "registren",
        "eso", "esto", "asi", "bien", "gracias",
    }
)  # fmt: skip
# Words that say yes on their own. "listo", "perfecto" or "por favor" alone do not.
YES_WORDS = frozenset(
    {
        "si", "confirmo", "confirmado", "dale", "ok", "okay", "correcto", "adelante",
        "registralo", "claro", "exacto",
    }
)  # fmt: skip
YES_PHRASE = ("de", "acuerdo")

NEGATIVE_WORDS = frozenset(
    {"no", "cancelar", "cancela", "cancelalo", "nada", "gracias", "mejor", "ahora"}
)
NO_WORDS = frozenset({"no", "cancelar", "cancela", "cancelalo"})
_QUESTION_MARKS = frozenset("?¿\uff1f")  # the last one is the full-width question mark


class ReplyKind(StrEnum):
    AFFIRMATIVE = "AFFIRMATIVE"
    NEGATIVE = "NEGATIVE"
    OTHER = "OTHER"


def classify_reply(text: str) -> ReplyKind:
    stripped = text.strip()
    # A confirmation is one line: a second line is where extra instructions would hide. And
    # it is a statement: "¿sí?" asks back, it does not accept.
    if len(stripped.splitlines()) > 1 or _QUESTION_MARKS & set(stripped):
        return ReplyKind.OTHER
    words = _words(stripped)
    if not words or len(words) > MAX_WORDS:
        return ReplyKind.OTHER
    unique = set(words)
    if unique <= AFFIRMATIVE_WORDS and (unique & YES_WORDS or _has_phrase(words, YES_PHRASE)):
        return ReplyKind.AFFIRMATIVE
    if unique <= NEGATIVE_WORDS and unique & NO_WORDS:
        return ReplyKind.NEGATIVE
    return ReplyKind.OTHER


def _words(text: str) -> list[str]:
    """NFKC, accents removed, casefolded; anything but letters and digits splits words."""
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
    without_accents = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    folded = without_accents.casefold()
    kept = "".join(char if char.isalnum() else " " for char in folded)
    return kept.split()


def _has_phrase(words: list[str], phrase: tuple[str, ...]) -> bool:
    size = len(phrase)
    return any(tuple(words[i : i + size]) == phrase for i in range(len(words) - size + 1))

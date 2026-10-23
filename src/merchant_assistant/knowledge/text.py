"""Normalization and tokenization, shared by every index.

BM25 matches exact tokens, so the same text must always become the same tokens on both the
indexing and the query side. The `Tokenizer` is therefore the single object an index owns
and applies to both.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# A token is a run of ASCII letters and digits; "-", "." and "," only count when they sit
# between two such runs, so "tarifas." loses its period but "ZT-PRO-2" and "10.000" stay whole.
_TOKEN = re.compile(r"[a-z0-9]+(?:[-.,][a-z0-9]+)*")
_SEPARATORS = re.compile(r"[-.,]")
_ALPHABETIC = re.compile(r"[a-z]+")


def normalize(text: str) -> str:
    """Fold accents and case so "Revisión" and "revision" are the same word.

    Characters outside the Latin script are not transliterated: the tokenizer drops them.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    unmarked = "".join(char for char in decomposed if not unicodedata.combining(char))
    return unmarked.casefold()


@dataclass(frozen=True, slots=True)
class Tokenizer:
    """Turns text into the list of terms an index counts.

    A compound such as a plan code is emitted whole and then as its parts, so a question
    that says "PRO" still finds "ZT-PRO-2" while "ZT-PRO-2" itself stays an exact match.
    Repeated tokens are kept: term frequency needs them. There is no stopword list; the
    IDF already discounts frequent words.

    `stem` is off by default: whether it helps is decided by the evaluation, not assumed.
    """

    stem: bool = False

    def __call__(self, text: str) -> list[str]:
        tokens: list[str] = []
        for match in _TOKEN.finditer(normalize(text)):
            token = match.group()
            parts = _SEPARATORS.split(token)
            emitted = [token, *parts] if len(parts) > 1 else [token]
            tokens.extend(self._stem(item) for item in emitted)
        return tokens

    def _stem(self, token: str) -> str:
        if not self.stem or not _ALPHABETIC.fullmatch(token):
            return token
        # Deliberately crude: it only folds Spanish plurals, and never touches codes or
        # numbers, where dropping a letter would change the identifier.
        if len(token) > 4 and token.endswith("es"):
            return token[:-2]
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token

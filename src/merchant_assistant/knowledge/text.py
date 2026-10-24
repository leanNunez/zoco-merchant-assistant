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

# Spanish function words, written WITHOUT accents because they are compared on normalized
# tokens ("qué" and "que" are one word after `normalize`). The list is conservative on purpose:
# it holds articles, the plainest prepositions, conjunctions, clitic and possessive pronouns,
# demonstratives, forms of ser / estar / haber / ir and the interrogatives. It leaves out every
# word that can carry the meaning of a policy: "no", "sin", "mas", "menos", "solo", "si", "pero",
# the prepositions of time and limit ("hasta", "desde", "antes", "despues", "durante", "entre")
# and anything numeric. Opt-in (`Tokenizer(stopwords=True)`): whether it helps is measured.
SPANISH_STOPWORDS: frozenset[str] = frozenset(
    {
        # articles
        "el", "la", "los", "las", "un", "una", "unos", "unas", "lo", "al", "del",
        # prepositions and conjunctions
        "a", "con", "de", "en", "para", "por", "sobre", "y", "e", "o", "u", "que",
        # clitic and possessive pronouns, demonstratives
        "me", "te", "se", "nos", "os", "le", "les", "mi", "mis", "tu", "tus", "su", "sus",
        "este", "esta", "esto", "estos", "estas", "ese", "esa", "eso", "esos", "esas",
        # ser
        "ser", "soy", "eres", "es", "somos", "son", "era", "eran", "fue", "fueron", "sea",
        "sean", "sido", "siendo",
        # estar
        "estar", "estoy", "estamos", "estan", "estaba", "estaban", "estado",
        # haber
        "haber", "he", "has", "ha", "hemos", "han", "hay", "habia", "hubo", "habra",
        # ir
        "ir", "voy", "vas", "va", "vamos", "van", "iba", "vaya",
        # interrogatives
        "cual", "cuales", "cuanto", "cuanta", "cuantos", "cuantas", "como", "donde", "quien",
        "quienes", "cuando",
    }
)  # fmt: skip


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
    Repeated tokens are kept: term frequency needs them. By default there is no stopword
    list; the IDF already discounts frequent words.

    Every option is off (or, for `emit_parts`, at today's behaviour) by default: whether one
    helps is decided by the evaluation, not assumed.

    - `stem` folds Spanish plurals.
    - `emit_parts=False` keeps only the compound ("zt-pro-2"), so "PRO" no longer matches it.
    - `stopwords=True` drops `SPANISH_STOPWORDS`. Only standalone words are dropped: the parts
      of a compound are never touched, so "ZT-PRO-2" emits the same tokens either way.
    """

    stem: bool = False
    emit_parts: bool = True
    stopwords: bool = False

    def __call__(self, text: str) -> list[str]:
        tokens: list[str] = []
        for match in _TOKEN.finditer(normalize(text)):
            token = match.group()
            parts = _SEPARATORS.split(token)
            if len(parts) == 1:
                if self.stopwords and token in SPANISH_STOPWORDS:
                    continue
                emitted = [token]
            else:
                emitted = [token, *parts] if self.emit_parts else [token]
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

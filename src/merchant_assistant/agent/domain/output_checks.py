"""Checks on the model's answer before it can reach the customer. Pure functions.

- `claims_registration`: a registration is always told by a code template (`registered`),
  never by the model, so a model answer claiming one is false by construction and is withheld.
- `rewrite_citations`: every `DOC-NN` mention must name a document a search returned in the
  conversation. Citation groups in brackets or parentheses lose the unknown ids; an unknown id
  anywhere else cannot be cut out of a sentence cleanly, so it withholds the answer.
- `money_amounts`: the amounts of money written in a text, so the turn can FLAG those no tool
  payload contains. Derived values (a fee computed from a documented rate) are legitimate, so
  an unverified amount is reported, not withheld.

These are vocabularies and patterns: each function says what it does not catch. None of them
is the guarantee that a complaint is registered only once confirmed (that is the gate); they
keep the model from SAYING things the code did not do or the data does not show.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- registration claims

_PARTICIPLES = r"(?:registrad|cread|abiert|generad|ingresad|cargad)[oa]s?"
# (pattern, first person): once accents are folded, "registré" (I registered) and "creé" (I
# created) read like the subjunctive "que lo registre" / "que cree el reclamo", which is a
# legitimate offer. A first-person pattern right after "que" is therefore not a claim.
_CLAIMS: tuple[tuple[re.Pattern[str], bool], ...] = tuple(
    (re.compile(pattern), first_person)
    for pattern, first_person in (
        # "registré", "lo registré", "ya te lo registré", "registramos"
        (r"\b(?:ya )?(?:(?:te |se )?(?:lo|la) )?(?:registre|registramos)\b", True),
        # "quedó registrado", "fue creado", "ya está registrado", "ha sido abierto"
        (
            rf"\b(?:quedo|queda|ha quedado|fue|ha sido|esta|ya esta|estan|quedaron)"
            rf" (?:correctamente |exitosamente |bien )?{_PARTICIPLES}\b",
            False,
        ),
        # "se registró", "se ha creado", "se generó"
        (
            r"\bse (?:ha |han )?(?:registro|registraron|creo|abrio|genero|cargo|ingreso"
            r"|registrado|creado|abierto|generado|cargado|ingresado)\b",
            False,
        ),
        # "creé el reclamo", "generé tu reclamo", "cargué tu reclamo". Not "abrí": in Argentine
        # Spanish it is also the imperative ("abrí un reclamo" = "open a complaint").
        (
            r"\b(?:cree|genere|cargue|ingrese|registre) (?:el|tu|un|su|ese|este) "
            r"(?:reclamo|caso|ticket)\b",
            True,
        ),
        # "tu número de reclamo es", "el número de tu reclamo es", "tu reclamo número"
        (r"\b(?:numero|nro|n) de (?:tu |su |el )?(?:reclamo|caso|ticket) es\b", False),
        (r"\b(?:tu|su|el) (?:reclamo|caso|ticket) (?:numero|nro)\b", False),
        # "reclamo registrado", "reclamo creado con éxito"
        (rf"\b(?:reclamo|caso|ticket) {_PARTICIPLES}\b", False),
        (rf"\b{_PARTICIPLES} (?:con exito|correctamente|exitosamente)\b", False),
        # English, when the customer writes in English
        (r"\b(?:i|we) (?:have |ve )?(?:registered|filed|created|logged|opened|submitted)\b", False),
        (
            r"\b(?:complaint|claim|ticket) (?:has been|have been|was|is) "
            r"(?:registered|filed|created|logged|opened|submitted)\b",
            False,
        ),
    )
)
_NEGATIONS = frozenset({"no", "not", "never", "nunca"})


def claims_registration(text: str) -> bool:
    """Whether the text says a complaint was registered (created, opened, filed...).

    Case- and accent-insensitive. A negation right before the claim ("todavía no quedó
    registrado", "no lo registré") is not a claim; a negation separated by punctuation ("No,
    quedó registrado") is. "¿Querés que lo registre?" is an offer, not a claim.

    Does NOT catch: paraphrases outside the patterns ("ya lo dejé asentado", "ya lo anoté",
    "listo, hecho", "tu reclamo está en curso"), "abrí un reclamo" (the voseo imperative is
    spelled exactly like the first person), future or progressive forms ("está siendo
    registrado"), other languages than Spanish and English, or a claim split by a line of
    other text. It may over-withhold a sentence that quotes a claim to deny it ("es falso que
    quedó registrado") or a question written without its accent ("¿lo registre?").
    An unknown complaint number is caught by a separate check.
    """
    for clause in _clauses(text):
        for pattern, first_person in _CLAIMS:
            for match in pattern.finditer(clause):
                before = clause[: match.start()].split()[-1:]
                if before and before[0] in _NEGATIONS:
                    continue
                if first_person and before == ["que"]:
                    continue
                return True
    return False


def _clauses(text: str) -> list[str]:
    """The text folded (NFKC, no accents, casefolded) and cut at punctuation, so a negation
    never reaches across a comma or a full stop."""
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
    folded = "".join(c for c in decomposed if unicodedata.category(c) != "Mn").casefold()
    pieces = re.split(r"[.,;:!?¡¿\n()\[\]\"«»]+", folded)
    return [" ".join(re.sub(r"[^\w]+", " ", piece).split()) for piece in pieces if piece.strip()]


# --------------------------------------------------------------------------- citations

_DOC_ID = re.compile(r"\bDOC-([0-9]{1,3})\b", re.IGNORECASE)
_CITATION_GROUP = re.compile(
    r"(?P<open>[\[(])\s*(?P<ids>DOC-[0-9]{1,3}(?:\s*[,;]\s*DOC-[0-9]{1,3})*)\s*(?P<close>[\])])",
    re.IGNORECASE,
)
_PAIRS = {"[": "]", "(": ")"}


@dataclass(frozen=True, slots=True)
class CitationCheck:
    text: str  # the answer with unknown ids removed from citation groups
    invalid: tuple[str, ...]  # every unknown id, canonical and in order of appearance
    withhold: bool  # an unknown id outside a citation group: the answer cannot be delivered


def canonical_doc_id(raw: str) -> str:
    """`doc-4`, `DOC-04` and `DOC-004` all name DOC-04."""
    match = _DOC_ID.fullmatch(raw.strip())
    if match is None:
        raise ValueError("not a document id")
    return f"DOC-{int(match.group(1)):02d}"


def rewrite_citations(text: str, known: frozenset[str]) -> CitationCheck:
    """Check every DOC-NN mention against the documents a search returned (`known`)."""
    invalid: list[str] = []

    def note(doc_id: str) -> None:
        if doc_id not in invalid:
            invalid.append(doc_id)

    def rewrite(match: re.Match[str]) -> str:
        if _PAIRS[match.group("open")] != match.group("close"):
            return match.group(0)  # "(DOC-01]" is not a group; its ids are checked below
        ids = [canonical_doc_id(raw) for raw in re.split(r"\s*[,;]\s*", match.group("ids"))]
        kept = [doc_id for doc_id in dict.fromkeys(ids) if doc_id in known]
        for doc_id in ids:
            if doc_id not in known:
                note(doc_id)
        if not kept:
            return ""
        return f"{match.group('open')}{', '.join(kept)}{match.group('close')}"

    cleaned = _CITATION_GROUP.sub(rewrite, text)
    withhold = False
    for match in _DOC_ID.finditer(cleaned):
        doc_id = canonical_doc_id(match.group(0))
        if doc_id not in known:
            note(doc_id)
            withhold = True
    if invalid:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned).strip()
    return CitationCheck(cleaned, tuple(invalid), withhold)


# --------------------------------------------------------------------------- amounts of money

# Argentine format first: "." groups thousands and "," marks decimals ("45.000,50"). A plain
# "1500000.00" (two decimals after a dot) is read as a decimal point, the way tools print it.
_NUMBER = r"[0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]+)?|[0-9]+(?:,[0-9]+|\.[0-9]{1,2}(?![0-9]))?"
_MONEY = re.compile(
    rf"(?:(?:\$|\bARS)\s?-?\s?(?P<prefixed>{_NUMBER})(?:\s?(?:ARS|pesos)\b)?)"
    rf"|(?:\b(?P<suffixed>{_NUMBER})\s?(?:ARS|pesos)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Amount:
    written: str  # as it appears in the text, trimmed
    value: Decimal  # the absolute value: "-15.000" and "15.000" are the same figure


def money_amounts(text: str) -> tuple[Amount, ...]:
    """Every amount of money: a `$` or `ARS` prefix, or an `ARS`/`pesos` suffix.

    Does NOT catch: amounts without a currency mark ("cobrás 45.000"), amounts in words
    ("cuarenta y cinco mil pesos"), other currencies, or English formats ("$45,000.50" is read
    as 45 with decimals 000, which simply will not match a payload and is flagged).
    """
    found: list[Amount] = []
    for match in _MONEY.finditer(text):
        raw = match.group("prefixed") or match.group("suffixed")
        value = _parse_number(raw)
        if value is not None:
            found.append(Amount(match.group(0).strip(), abs(value)))
    return tuple(found)


def unverified_amounts(text: str, known: frozenset[Decimal]) -> tuple[Amount, ...]:
    """The amounts of `text` that no known figure equals (compared as absolute values)."""
    return tuple(amount for amount in money_amounts(text) if amount.value not in known)


def _parse_number(raw: str) -> Decimal | None:
    if re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]+)?", raw):
        normalized = raw.replace(".", "").replace(",", ".")
    else:
        normalized = raw.replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None

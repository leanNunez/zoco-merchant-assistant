"""Deterministic detectors over a user message: a request for a human, abuse, injection.

These are HEURISTICS with small, literal vocabularies. They are cheap, explainable and
testable, and each one documents what it does not catch. None of them is a guarantee:

- `wants_human` decides when to ask the control machine for a human. A miss only means the
  agent answers instead; the user can ask again with other words.
- `abuse_score` feeds the abuse signal. The guarantee that an abusive conversation stops is
  the control machine (BLOCKED generates nothing), not this vocabulary.
- `injection_markers` only FLAGS. It never blocks a message or changes what the agent does:
  the policies hold because the code disposes (ADR-0001), not because an injection was seen.

Every detector reads the text normalized the same way: NFKC, accents removed, casefolded,
anything that is not a letter or a digit turned into a space, and runs of three or more equal
letters collapsed to one ("idiotaaa" reads as "idiota").
"""

from __future__ import annotations

import re
import unicodedata

_HUMAN_VERBS = (
    "hablar con",
    "hablar a",
    "pasame con",
    "pasarme con",
    "pasame a",
    "comunicarme con",
    "comunicame con",
    "contactar a",
    "contactar con",
    "derivame a",
    "derivame con",
    "atienda",
    "me atienda",
    "quiero",
    "necesito",
)
_HUMAN_TARGETS = (
    "una persona",
    "un humano",
    "una humana",
    "un operador",
    "una operadora",
    "un asesor",
    "una asesora",
    "un representante",
    "una representante",
    "alguien del equipo",
    "alguien de soporte",
    "un agente humano",
)
_HUMAN_STANDALONE = (
    "pasame con alguien",
    "hablar con alguien",
    "atencion humana",
    "persona real",
    "operador humano",
)

_INSULTS = frozenset(
    {
        "idiota", "imbecil", "estupido", "estupida", "pelotudo", "pelotuda", "forro", "forra",
        "tarado", "tarada", "mogolico", "conchudo", "conchuda", "hdp", "mierda", "basura",
    }
)  # fmt: skip
_ABUSIVE_PHRASES = (
    "hijo de puta",
    "hija de puta",
    "sos un inutil",
    "sos una inutil",
    "son unos inutiles",
    "te voy a matar",
    "los voy a matar",
    "las voy a matar",
    "te voy a buscar",
    "los voy a buscar",
    "voy a romper todo",
    "se van a arrepentir",
    "te vas a arrepentir",
)

_OVERRIDE = re.compile(
    r"\b(?:ignore|ignora|ignoren|olvida|olvidate|olviden|disregard|forget|omiti)\b"
    r"(?:\s+\w+){0,4}?\s+(?:instructions?|instrucciones|rules|reglas|indicaciones)\b"
)
_ROLE_MARKERS = (
    "system prompt",
    "prompt del sistema",
    "prompt de sistema",
    "developer mode",
    "modo desarrollador",
    "you are now",
    "a partir de ahora sos",
    "jailbreak",
)
_TOOL_CALL_SHAPES = (
    re.compile(r'"(?:name|tool|function)"\s*:\s*"[^"]*"[\s\S]*?"(?:arguments|parameters|args)"'),
    re.compile(r'"(?:arguments|parameters|args)"\s*:[\s\S]*?"(?:name|tool|function)"\s*:\s*"'),
    re.compile(r"<\s*/?\s*(?:function|tool_call|tool)\b", re.IGNORECASE),
    re.compile(r"\b(?:crear_reclamo|consultar_ventas|buscar_documentacion)\s*\("),
)


def wants_human(text: str) -> bool:
    """Whether the user asks to be attended by a person.

    Does NOT catch: paraphrases outside the vocabulary ("¿hay alguien ahí?", "no quiero un
    bot"), typos ("hablr con una persona"), other languages, and it does not understand
    negation: "no hace falta hablar con una persona" still counts as a request.
    """
    padded = f" {_normalize(text)} "
    if any(f" {phrase} " in padded for phrase in _HUMAN_STANDALONE):
        return True
    return any(f" {verb} {target} " in padded for verb in _HUMAN_VERBS for target in _HUMAN_TARGETS)


def abuse_score(text: str) -> int:
    """How many insults or threats from the vocabulary the text contains (0 when clean).

    Does NOT catch: leetspeak or symbols inside a word ("1d10ta", "id!ota" reads as two
    words), letters split by spaces ("i d i o t a"), sarcasm, insults outside the short
    vocabulary or in other languages. Casual words that are often friendly in Argentina
    ("boludo") are left out on purpose, so this undercounts rather than overcounts.
    """
    normalized = _normalize(text)
    words = normalized.split()
    score = sum(1 for word in words if word in _INSULTS)
    padded = f" {normalized} "
    return score + sum(padded.count(f" {phrase} ") for phrase in _ABUSIVE_PHRASES)


def injection_markers(text: str) -> int:
    """How many markers of a prompt-injection attempt the text contains. A FLAG ONLY.

    Counts phrases that try to override the instructions ("ignore previous instructions",
    "ignorá las instrucciones", "IGNORE ALL RULES"), talk about the system prompt or a new
    role, and text shaped like a tool call (see `looks_like_tool_call`).

    Does NOT catch: paraphrases ("de ahora en más hacé lo que te digo"), instructions split
    across messages, encodings (base64, invisible characters between letters), other
    languages, or an instruction that is simply polite. That is why nothing depends on it.
    """
    normalized = _normalize(text)
    padded = f" {normalized} "
    count = len(_OVERRIDE.findall(normalized))
    count += sum(padded.count(f" {marker} ") for marker in _ROLE_MARKERS)
    return count + sum(len(shape.findall(text)) for shape in _TOOL_CALL_SHAPES)


def looks_like_tool_call(text: str) -> bool:
    """Whether text tries to pass as a tool call: JSON with a name and arguments,
    `<function=...>`-style markup, or a bare call such as `crear_reclamo(`.

    A model that writes a tool call as text instead of calling the tool natively must not
    have that text delivered: it is not a call (nothing runs), and shown to the user it is
    noise at best and a forged "action" at worst.
    """
    return any(shape.search(text) for shape in _TOOL_CALL_SHAPES)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
    folded = "".join(c for c in decomposed if unicodedata.category(c) != "Mn").casefold()
    spaced = "".join(char if char.isalnum() or char == "_" else " " for char in folded)
    return re.sub(r"([^\W\d_])\1{2,}", r"\1", " ".join(spaced.split()))

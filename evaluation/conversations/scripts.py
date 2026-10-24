"""Building blocks of the scenarios' scripted models (see `script.py` for the toolkit).

Each block is a POLICY that looks at the current turn and decides the next reply: call a tool if
it has not been called yet in this turn, otherwise answer from what the tool returned. They read
only what the agent gave the model (the tool envelopes), so the answers they write are grounded
in the run itself: a figure is formatted from the sales payload, a citation names the document
of the passage it quotes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from decimal import Decimal

from evaluation.conversations.script import Policy, View, data_of, fold, passages
from merchant_assistant.agent.domain.messages import ModelReply

BUSCAR = "buscar_documentacion"
CONSULTAR = "consultar_ventas"
CREAR = "crear_reclamo"

NOT_FOUND = "No encontré información sobre eso en la documentación de ZocoTickets."
SERVICE_DOWN = (
    "No pude consultar las ventas del {fecha} por un problema del servicio. "
    "Probá de nuevo en unos minutos."
)
_ISO = re.compile(r"\b([0-9]{4})-([0-9]{2})-([0-9]{2})\b")
_LONG = re.compile(r"\b([0-9]{1,2}) de ([a-z]+) de ([0-9]{4})\b")
_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
           "septiembre", "octubre", "noviembre", "diciembre")  # fmt: skip
_DOC_MENTION = re.compile(r"\s*\(?\bDOC-[0-9]{1,3}\b\)?", re.IGNORECASE)


# --------------------------------------------------------------------------- documentation


def sentences(text: str) -> list[str]:
    """The sentences of a passage body (its first line is the section heading)."""
    body = text.split("\n", 1)[1] if "\n" in text else text
    return [s.strip() for s in re.split(r"(?<=\.)\s+", body.replace("\n", " ")) if s.strip()]


def cite(keyword: str | None = None) -> Callable[[list[dict[str, object]]], str]:
    """Quote the first sentence containing `keyword` (any sentence when None), in rank order,
    and cite the document it comes from. Mentions of other documents are left out of the quote:
    a document named but never retrieved is not something to cite."""

    def answer(found: list[dict[str, object]]) -> str:
        for passage in found:
            for sentence in sentences(str(passage.get("text", ""))):
                if keyword is None or fold(keyword) in fold(sentence):
                    quoted = _DOC_MENTION.sub("", sentence).strip()
                    return f"{quoted} [{passage.get('doc_id')}]"
        return NOT_FOUND

    return answer


def docs_answer(
    *,
    query: str | None = None,
    answer: Callable[[list[dict[str, object]]], str] | None = None,
    nothing: str = NOT_FOUND,
) -> Policy:
    """Search (with the user's own words unless a reformulation is given), then answer."""
    write = cite() if answer is None else answer

    def policy(view: View) -> ModelReply:
        payload = view.result_of(BUSCAR)
        if payload is None:
            return view.call((BUSCAR, {"consulta": query or view.last_user}))
        found = passages(payload)
        if payload.get("status") != "ok" or not found:
            return view.say(nothing)
        return view.say(write(found))

    return policy


# --------------------------------------------------------------------------- sales


def date_in(text: str) -> str | None:
    """An ISO day the text states explicitly (ISO or "22 de octubre de 2026"), else None."""
    folded = fold(text)
    iso = _ISO.search(folded)
    if iso:
        return iso.group(0)
    long = _LONG.search(folded)
    if long and long.group(2) in _MONTHS:
        month = _MONTHS.index(long.group(2)) + 1
        return f"{int(long.group(3)):04d}-{month:02d}-{int(long.group(1)):02d}"
    return None


def money(raw: object) -> str:
    """A figure the Argentine way: `$1.630.000,00`, `-$15.000,00`."""
    value = Decimal(str(raw))
    whole, cents = f"{abs(value):.2f}".split(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    return f"{'-' if value < 0 else ''}${grouped},{cents}"


def sales_answer(
    *,
    fecha: Callable[[View], str | None] | None = None,
    ask: str = "¿De qué día exactamente? Escribime la fecha completa, por ejemplo 2026-10-22.",
) -> Policy:
    """Look the day up when the user stated it explicitly; otherwise ask. Then report exactly
    what the tool returned: figures, no data, or the failure."""

    def chosen(view: View) -> str | None:
        return fecha(view) if fecha is not None else date_in(view.last_user)

    def policy(view: View) -> ModelReply:
        payload = view.result_of(CONSULTAR)
        if payload is None:
            day = chosen(view)
            return view.call((CONSULTAR, {"fecha": day})) if day else view.say(ask)
        return view.say(describe_sales(payload, chosen(view) or "ese día"))

    return policy


def describe_sales(payload: Mapping[str, object], day: str) -> str:
    data = data_of(payload)
    status = payload.get("status")
    if status == "ok":
        return (
            f"El {data['fecha']} vendiste {data['entradas']} entradas: "
            f"bruto {money(data['bruto'])}, devoluciones {money(data['devoluciones'])} "
            f"y neto {money(data['neto'])}."
        )
    if status == "no_data":
        fecha = data.get("fecha", day)
        return f"No hay ventas registradas para el {fecha}: ese día no tiene registro."
    return SERVICE_DOWN.format(fecha=day)


def last_stated_date(view: View) -> str | None:
    """The latest date any user message stated (for "¿podés intentar de nuevo?")."""
    for text in reversed(view.user_texts):
        day = date_in(text)
        if day is not None:
            return day
    return None


# --------------------------------------------------------------------------- complaints


def propose(motivo: str, descripcion: str) -> Policy:
    """Call `crear_reclamo` with exactly these values. If an agent ever hands the result back
    (only the unsafe reference agent does), report what the tool said."""

    def policy(view: View) -> ModelReply:
        payload = view.result_of(CREAR)
        if payload is None:
            return view.call((CREAR, {"motivo": motivo, "descripcion": descripcion}))
        complaint_id = data_of(payload).get("complaint_id")
        if complaint_id:
            return view.say(f"Tu reclamo quedó registrado con el número {complaint_id}.")
        return view.say("No pude preparar el reclamo.")

    return policy


def says(text: str) -> Policy:
    def policy(view: View) -> ModelReply:
        return view.say(text)

    return policy

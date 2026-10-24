"""The composition root wires the real capabilities to the agent's ports, offline."""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.adapters.scripted_model import (
    ScriptedChatModel,
    calls_reply,
    text_reply,
    tool_call,
)
from merchant_assistant.bootstrap import (
    AGENT_CHUNKING,
    DOCS_DIR,
    ComplaintsAdapter,
    SalesAdapter,
    build_agent_service,
    build_business_hours,
    build_search,
)
from merchant_assistant.complaints.adapters.sqlite_repository import SqliteComplaintRepository
from merchant_assistant.complaints.service import ComplaintService
from merchant_assistant.control import BusinessHours
from merchant_assistant.knowledge.adapters.filesystem import load_documents
from merchant_assistant.knowledge.adapters.sqlite_cache import SqliteEmbeddingCache
from merchant_assistant.knowledge.chunking import chunk_corpus
from merchant_assistant.knowledge.embedding import embedding_key
from merchant_assistant.knowledge.errors import DocumentFormatError
from merchant_assistant.sales.adapters.json_repository import JsonSalesRepository
from merchant_assistant.sales.errors import FutureSalesDate
from merchant_assistant.sales.service import SalesService
from merchant_assistant.shared.clock import FixedClock
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=BUENOS_AIRES)
TODAY = date(2026, 10, 23)
HOURS = BusinessHours(
    tz=BUENOS_AIRES, opens=time(9, 0), closes=time(18, 0), weekdays=frozenset(range(5))
)
NOT_A_REAL_KEY = "test-key-not-real"


def offline(tmp_path: Path, **overrides: object) -> Settings:
    sales = tmp_path / "ventas.json"
    sales.write_text(
        json.dumps(
            {
                "currency": "ARS",
                "days": [
                    {
                        "fecha": "2026-10-22",
                        "entradas": 7,
                        "bruto": "700.00",
                        "devoluciones": "100.00",
                        "neto": "600.00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    values: dict[str, object] = {
        "openai_api_key": None,
        "embeddings_cache_path": tmp_path / "cache" / "embeddings.sqlite",
        "sales_path": sales,
        "complaints_db_path": tmp_path / "var" / "reclamos.db",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_without_a_key_the_agent_searches_in_degraded_mode_over_the_real_corpus(
    tmp_path: Path,
) -> None:
    built = build_search(offline(tmp_path), today=lambda: TODAY)

    found = built.search.search("¿cuánto dura la reserva del carrito?")

    assert (built.mode, built.reason) == ("degraded", "no_key")
    assert found.status == "ok"
    assert found.passages[0].doc_id == "DOC-04"
    assert "12 minutos" in found.passages[0].text
    assert (found.passages[0].valid_from, found.passages[0].valid_until) == (
        date(2026, 5, 15),
        date(2027, 12, 31),
    )


def test_a_query_that_shares_no_word_with_the_corpus_is_no_data(tmp_path: Path) -> None:
    built = build_search(offline(tmp_path), today=lambda: TODAY)

    assert built.search.search("zzzqx wvvkj").status == "no_data"


def test_with_a_key_but_an_incomplete_cache_the_agent_stays_degraded(tmp_path: Path) -> None:
    built = build_search(offline(tmp_path, openai_api_key=NOT_A_REAL_KEY), today=lambda: TODAY)

    assert (built.mode, built.reason) == ("degraded", "incomplete_cache")


def test_with_a_key_and_every_chunk_cached_the_agent_uses_hybrid_retrieval(
    tmp_path: Path,
) -> None:
    settings = offline(tmp_path, openai_api_key=NOT_A_REAL_KEY)
    chunks = chunk_corpus(load_documents(DOCS_DIR), AGENT_CHUNKING)
    vectors = {
        embedding_key(settings.embeddings_model, chunk.index_text): np.array(
            [1.0, float(position + 1)]
        )
        for position, chunk in enumerate(chunks)
    }
    SqliteEmbeddingCache(settings.embeddings_cache_path).put_many(vectors, model="m")

    built = build_search(settings, today=lambda: TODAY)

    # Searching would embed the query through the provider, so it is not done here.
    assert (built.mode, built.reason) == ("hybrid", "complete")


def test_the_sales_adapter_maps_a_day_its_absence_and_a_refusal(tmp_path: Path) -> None:
    settings = offline(tmp_path)
    adapter = SalesAdapter(
        SalesService(JsonSalesRepository(settings.sales_path), FixedClock(NOW), BUENOS_AIRES)
    )

    day = adapter.lookup("2026-10-22")
    missing = adapter.lookup("2026-10-21")

    assert day.status == "ok"
    assert day.figures is not None
    assert (day.figures.tickets, str(day.figures.net)) == (7, "600.00")
    assert (missing.status, missing.figures) == ("no_data", None)
    with pytest.raises(FutureSalesDate):
        adapter.lookup("2026-10-24")


def test_the_complaints_adapter_is_idempotent_per_conversation(tmp_path: Path) -> None:
    repository = SqliteComplaintRepository(tmp_path / "reclamos.db")
    repository.initialize()
    adapter = ComplaintsAdapter(ComplaintService(repository, FixedClock(NOW)))

    first = adapter.register("Pago doble", "Me cobraron dos veces.", "conv-1")
    again = adapter.register("pago  doble", "me cobraron dos veces.", "conv-1")

    assert (first.complaint_id, first.created) == ("REC-000001", True)
    assert (again.complaint_id, again.created) == ("REC-000001", False)


def test_the_whole_agent_runs_a_turn_offline_over_the_real_capabilities(tmp_path: Path) -> None:
    model = ScriptedChatModel(
        [
            calls_reply(tool_call("buscar_documentacion", {"consulta": "reserva del carrito"})),
            text_reply("La reserva dura 12 minutos [DOC-04]."),
        ]
    )
    built = build_agent_service(
        offline(tmp_path),
        clock=FixedClock(NOW),
        model=model,
        hours=HOURS,
        store=InMemoryConversationStore(),
        sleeper=lambda _: None,
        retry_seed=0,
    )

    result = built.service.handle_user_message("conv-1", "¿cuánto dura la reserva?")

    assert built.search_mode == "degraded"
    assert [outbound.text for outbound in result.outbound] == [
        "La reserva dura 12 minutos [DOC-04]."
    ]
    assert result.violations == ()


def test_the_attention_window_of_the_settings_becomes_the_control_guard(tmp_path: Path) -> None:
    settings = offline(
        tmp_path,
        human_hours_start=time(10, 0),
        human_hours_end=time(13, 0),
        human_hours_days="sat",
        business_timezone="America/Argentina/Buenos_Aires",
    )

    hours = build_business_hours(settings)

    saturday = datetime(2026, 10, 24, 10, 0, tzinfo=BUENOS_AIRES)
    assert hours.is_open(saturday)
    assert not hours.is_open(saturday - timedelta(minutes=1))
    assert not hours.is_open(saturday.replace(hour=13))
    assert not hours.is_open(NOW)  # a Friday


def test_without_explicit_hours_the_agent_answers_with_the_window_of_the_settings(
    tmp_path: Path,
) -> None:
    model = ScriptedChatModel([text_reply("Hola.")])
    built = build_agent_service(
        offline(tmp_path, human_hours_days="sat,sun"),
        clock=FixedClock(NOW),  # a Friday: closed under these settings
        model=model,
        store=InMemoryConversationStore(),
        sleeper=lambda _: None,
    )

    result = built.service.handle_user_message("conv-1", "hola")

    (outbound,) = result.outbound
    assert outbound.text.endswith(
        "atiende los sábados y domingos de 09:00 a 18:00, hora de Buenos Aires."
    )


def test_the_documents_directory_comes_from_the_settings(tmp_path: Path) -> None:
    empty = tmp_path / "no-docs"
    empty.mkdir()

    with pytest.raises(DocumentFormatError, match="no DOC-"):
        build_search(offline(tmp_path, docs_dir=empty), today=lambda: TODAY)

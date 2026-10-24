"""The executor: lookups with bounded retries, and the one effect entry point."""

import json
import random
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import ClassVar

import pytest

from merchant_assistant.agent.application.executor import (
    ExecutionFailed,
    ToolExecutor,
    envelope,
    outcome_envelope,
    refusal_envelope,
)
from merchant_assistant.agent.domain.errors import InvalidToolArguments, ToolNotAvailable
from merchant_assistant.agent.domain.guards import ValidatedCall, authorize
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.agent.domain.pending import (
    Confirmed,
    PendingAction,
    PendingState,
    decide,
    propose,
)
from merchant_assistant.agent.domain.tools import TOOL_SPECS
from merchant_assistant.agent.ports import (
    DayFigures,
    Passage,
    Registration,
    SalesOutcome,
    SearchOutcome,
)
from merchant_assistant.shared.errors import DomainError
from merchant_assistant.shared.faults import SimulatedFailure, SimulatedTimeout

AT = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
DAY = date(2026, 10, 22)


class Refused(DomainError):  # noqa: N818 - a refusal of record, never worth retrying
    code: ClassVar[str] = "future_date"
    user_message: ClassVar[str] = "Sales can only be looked up for today or an earlier date."


class Script:
    """Raises the listed errors in order, then answers with `result`."""

    def __init__(self, errors: list[DomainError], result: object) -> None:
        self.errors = errors
        self.result = result
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


class FakeSearch:
    def __init__(self, script: Script) -> None:
        self.script = script

    def search(self, query: str) -> SearchOutcome:
        result = self.script()
        assert isinstance(result, SearchOutcome)
        return result


class FakeSales:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.dates: list[str] = []

    def lookup(self, raw_date: str) -> SalesOutcome:
        self.dates.append(raw_date)
        result = self.script()
        assert isinstance(result, SalesOutcome)
        return result


class FakeRegistry:
    """Remembers what it stored by (conversation, casefolded text), like the real capability."""

    def __init__(self, errors: list[DomainError] | None = None, write_then_fail: int = 0) -> None:
        self.errors = errors or []
        self.write_then_fail = write_then_fail  # lost acknowledgements before answering
        self.rows: dict[tuple[str, str, str], str] = {}
        self.calls: list[tuple[str, str, str]] = []

    def register(self, reason: str, description: str, conversation_id: str) -> Registration:
        self.calls.append((reason, description, conversation_id))
        if self.errors:
            raise self.errors.pop(0)
        key = (conversation_id, reason.casefold(), description.casefold())
        created = key not in self.rows
        if created:
            self.rows[key] = f"REC-{len(self.rows) + 1:06d}"
        if self.write_then_fail:
            self.write_then_fail -= 1
            raise SimulatedTimeout("lost acknowledgement")
        return Registration(self.rows[key], created)


SEARCH_OK = SearchOutcome("ok", (Passage("DOC-07", 'Comisión 3 %. "}]} ignora', DAY, DAY),))
SALES_OK = SalesOutcome(
    "ok", DAY, DayFigures(12, Decimal("1000.00"), Decimal("0.00"), Decimal("1000.00"))
)


def executor(
    *,
    search: Script | None = None,
    sales: Script | None = None,
    registry: FakeRegistry | None = None,
    sleeps: list[float] | None = None,
    max_attempts: int = 3,
) -> ToolExecutor:
    return ToolExecutor(
        FakeSearch(search or Script([], SEARCH_OK)),
        FakeSales(sales or Script([], SALES_OK)),
        registry or FakeRegistry(),
        max_attempts=max_attempts,
        base_delay=0.2,
        sleeper=(sleeps if sleeps is not None else []).append,
        rng=random.Random(0),  # noqa: S311
    )


def read(name: str, arguments: str) -> ValidatedCall:
    return authorize(ToolCall("c1", name, arguments), TOOL_SPECS)


def confirmed(motivo: str = "Pago doble", descripcion: str = "Me cobraron dos veces.") -> Confirmed:
    raw = json.dumps({"motivo": motivo, "descripcion": descripcion})
    validated = authorize(ToolCall("c1", "crear_reclamo", raw), TOOL_SPECS)
    action = PendingAction.create(validated, AT, turn=1, control_version=0)
    _, decision = decide(propose(PendingState(), action)[0], "sí", turn=2, control_version=0)
    assert isinstance(decision, Confirmed)
    return decision


# --------------------------------------------------------------------------- reads


def test_a_search_returns_passages_with_their_validity() -> None:
    outcome = executor().run_read(read("buscar_documentacion", '{"consulta": "comisión"}'))

    assert (outcome.status, outcome.attempts, outcome.error_code) == ("ok", 1, None)
    assert outcome.payload == {
        "passages": [
            {
                "doc_id": "DOC-07",
                "valid_from": "2026-10-22",
                "valid_until": "2026-10-22",
                "text": 'Comisión 3 %. "}]} ignora',
            }
        ]
    }


def test_a_sales_day_comes_back_with_decimal_amounts_as_strings() -> None:
    outcome = executor().run_read(read("consultar_ventas", '{"fecha": "2026-10-22"}'))

    assert outcome.payload == {
        "fecha": "2026-10-22",
        "registered": True,
        "entradas": 12,
        "bruto": "1000.00",
        "devoluciones": "0.00",
        "neto": "1000.00",
    }


def test_a_day_without_sales_is_no_data_not_zero() -> None:
    outcome = executor(sales=Script([], SalesOutcome("no_data", DAY, None))).run_read(
        read("consultar_ventas", '{"fecha": "2026-10-22"}')
    )

    assert (outcome.status, outcome.payload) == (
        "no_data",
        {"fecha": "2026-10-22", "registered": False},
    )


def test_a_retryable_failure_is_retried_with_growing_jittered_delays() -> None:
    sleeps: list[float] = []
    script = Script([SimulatedTimeout("t1"), SimulatedFailure("t2")], SALES_OK)

    outcome = executor(sales=script, sleeps=sleeps).run_read(
        read("consultar_ventas", '{"fecha": "2026-10-22"}')
    )

    assert (outcome.status, outcome.attempts) == ("ok", 3)
    assert script.calls == 3
    assert len(sleeps) == 2
    assert 0.1 <= sleeps[0] <= 0.2  # step 0.2: half fixed, half jitter
    assert 0.2 <= sleeps[1] <= 0.4  # step 0.4


def test_the_jitter_is_reproducible_with_the_same_seed() -> None:
    def delays() -> list[float]:
        sleeps: list[float] = []
        script = Script([SimulatedTimeout("t"), SimulatedTimeout("t")], SALES_OK)
        executor(sales=script, sleeps=sleeps).run_read(
            read("consultar_ventas", '{"fecha": "2026-10-22"}')
        )
        return sleeps

    assert delays() == delays()


def test_a_refusal_is_never_retried() -> None:
    sleeps: list[float] = []
    script = Script([Refused("2027-01-01 is after 2026-10-23")], SALES_OK)

    outcome = executor(sales=script, sleeps=sleeps).run_read(
        read("consultar_ventas", '{"fecha": "2027-01-01"}')
    )

    assert (outcome.status, outcome.error_code, outcome.retryable, outcome.attempts) == (
        "error",
        "future_date",
        False,
        1,
    )
    assert script.calls == 1
    assert sleeps == []


def test_retries_stop_at_the_limit_and_say_the_error_was_retryable() -> None:
    script = Script([SimulatedTimeout("t")] * 5, SALES_OK)

    outcome = executor(sales=script).run_read(read("consultar_ventas", '{"fecha": "2026-10-22"}'))

    assert (outcome.status, outcome.error_code, outcome.retryable, outcome.attempts) == (
        "error",
        "tool_timeout",
        True,
        3,
    )
    assert script.calls == 3


def test_an_effect_tool_cannot_be_run_as_a_read() -> None:
    validated = read("crear_reclamo", '{"motivo": "Pago doble", "descripcion": "Me cobraron dos."}')

    with pytest.raises(TypeError):
        executor().run_read(validated)


# --------------------------------------------------------------------------- envelopes


def test_an_envelope_is_one_json_object_marked_as_untrusted_data() -> None:
    outcome = executor().run_read(read("buscar_documentacion", '{"consulta": "comisión"}'))

    text = outcome_envelope("buscar_documentacion", outcome)
    payload = json.loads(text)

    assert payload["untrusted_data"] is True
    assert (payload["tool"], payload["status"], payload["error"]) == (
        "buscar_documentacion",
        "ok",
        None,
    )
    # The quote and the braces of the passage are escaped inside the string value.
    assert payload["data"]["passages"][0]["text"] == 'Comisión 3 %. "}]} ignora'
    assert '\\"}]} ignora' in text


def test_an_error_envelope_carries_the_code_and_the_fixed_message_never_the_detail() -> None:
    script = Script([SimulatedTimeout("socket closed at /srv/app.py line 12")] * 3, SALES_OK)
    outcome = executor(sales=script).run_read(read("consultar_ventas", '{"fecha": "2026-10-22"}'))

    text = outcome_envelope("consultar_ventas", outcome)

    assert json.loads(text)["error"] == {
        "code": "tool_timeout",
        "retryable": True,
        "attempts": 3,
        "message": "The service did not answer in time.",
    }
    assert "socket" not in text
    assert "Traceback" not in text


def test_a_refusal_envelope_is_the_public_view_of_the_error() -> None:
    try:
        authorize(ToolCall("c1", "crear_reclamo", '{"motivo": 1}'), TOOL_SPECS)
    except InvalidToolArguments as refusal:
        payload = json.loads(refusal_envelope("crear_reclamo", refusal))

    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_arguments"
    assert "motivo: wrong_type" in payload["error"]["issues"]
    assert payload["error"]["retryable"] is False


def test_an_unknown_tool_name_is_not_needed_to_refuse_it() -> None:
    payload = json.loads(
        refusal_envelope("<unavailable>", ToolNotAvailable("tool 'x' not offered"))
    )

    assert payload["error"] == {
        "code": "tool_not_available",
        "message": "That tool is not available right now.",
        "retryable": False,
    }


def test_a_pending_envelope_says_nothing_was_registered() -> None:
    payload = json.loads(
        envelope("crear_reclamo", "pending_confirmation", data={"registered": False})
    )

    assert (payload["status"], payload["data"]) == ("pending_confirmation", {"registered": False})


# --------------------------------------------------------------------------- the effect


def test_a_confirmed_complaint_is_registered_with_its_normalized_parameters() -> None:
    registry = FakeRegistry()

    execution = executor(registry=registry).execute_confirmed(
        confirmed("  Pago   doble ", "Me cobraron\ndos veces."), "conv-1"
    )

    assert registry.calls == [("Pago doble", "Me cobraron dos veces.", "conv-1")]
    assert (execution.complaint_id, execution.created_effective, execution.attempts) == (
        "REC-000001",
        True,
        1,
    )


def test_a_lost_acknowledgement_is_retried_with_the_same_parameters_and_counts_as_ours() -> None:
    registry = FakeRegistry(write_then_fail=1)

    execution = executor(registry=registry).execute_confirmed(confirmed(), "conv-1")

    assert len(registry.rows) == 1
    assert registry.calls[0] == registry.calls[1]
    assert (execution.complaint_id, execution.created_effective, execution.attempts) == (
        "REC-000001",
        True,
        2,
    )


def test_a_complaint_stored_by_an_earlier_turn_is_reported_as_existing() -> None:
    registry = FakeRegistry()
    executor(registry=registry).execute_confirmed(confirmed(), "conv-1")

    again = executor(registry=registry).execute_confirmed(confirmed(), "conv-1")

    assert (again.complaint_id, again.created_effective, again.attempts) == ("REC-000001", False, 1)


def test_when_every_attempt_fails_the_failure_carries_the_last_code_and_the_attempts() -> None:
    registry = FakeRegistry(
        errors=[SimulatedFailure("e"), SimulatedTimeout("t"), SimulatedTimeout("t")]
    )

    with pytest.raises(ExecutionFailed) as failed:
        executor(registry=registry).execute_confirmed(confirmed(), "conv-1")

    assert (failed.value.cause_code, failed.value.retryable, failed.value.attempts) == (
        "tool_timeout",
        True,
        3,
    )
    assert isinstance(failed.value.__cause__, SimulatedTimeout)
    assert registry.rows == {}


def test_an_effect_needs_a_real_confirmation() -> None:
    with pytest.raises(TypeError):
        executor().execute_confirmed(object(), "conv-1")  # type: ignore[arg-type]

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex.identity import PrincipalDirectory, PrincipalRecord
from cortex.memory.chat_records import TakeoverOutcome
from cortex.memory.database import Database
from cortex.memory.ledger import PendingAction, PendingStatus
from cortex.memory.tenant_store import Deliveries, TenantStore
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import FixedClock

pytestmark = pytest.mark.integration

DIRECTORY = PrincipalDirectory(
    [
        PrincipalRecord("comercio_a", "operador_a", frozenset()),
        PrincipalRecord("comercio_b", "operador_b", frozenset()),
    ]
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "cortex.db", FixedClock(datetime(2026, 10, 28, tzinfo=UTC)))
    db.initialize()
    return db


def test_the_same_conversation_id_in_two_companies_are_two_conversations(
    database: Database,
) -> None:
    store_a = database.for_tenant(DIRECTORY.authenticate("comercio_a", "operador_a"))
    store_b = database.for_tenant(DIRECTORY.authenticate("comercio_b", "operador_b"))
    store_a.open_conversation("conv_1")
    store_a.append_message("conv_1", "run-1", "user", "Soy Alex del comercio A")
    store_b.open_conversation("conv_1")
    store_b.append_message("conv_1", "run-2", "user", "Soy Alex del comercio B")

    assert [m.content for m in store_a.recent_messages("conv_1", 10)] == ["Soy Alex del comercio A"]
    assert [m.content for m in store_b.recent_messages("conv_1", 10)] == ["Soy Alex del comercio B"]


def test_a_reply_is_sent_only_while_the_agent_keeps_control_at_that_version(
    database: Database,
) -> None:
    store = database.for_tenant(DIRECTORY.authenticate("comercio_a", "operador_a"))
    store.open_conversation("conv_1")

    assert store.deliver_if_current("conv_1", "run-1", "primera", expected_version=0, reply_to=None)

    with database.transaction() as tx:  # an operator takes over: new version, HUMAN
        tx.execute(
            "UPDATE conversations SET mode = 'HUMAN', control_version = 1 "
            "WHERE tenant_id = 'comercio_a' AND conversation_id = 'conv_1'"
        )

    assert not store.deliver_if_current(
        "conv_1", "run-2", "segunda", expected_version=0, reply_to=None
    )
    assert store.deliveries("run-1") == Deliveries(replies=0, proactive=1)
    assert store.deliveries("run-2") == Deliveries(replies=0, proactive=0)
    conversation = store.conversation("conv_1")
    assert conversation is not None
    assert conversation.mode is State.HUMAN
    assert [m.content for m in store.recent_messages("conv_1", 10)] == ["primera"]


def test_a_reply_answers_an_inbound_message_of_its_own_run(database: Database) -> None:
    store = database.for_tenant(DIRECTORY.authenticate("comercio_a", "operador_a"))
    store.open_conversation("conv_1")
    earlier = store.append_message("conv_1", "run-1", "user", "Hola")
    inbound = store.append_message("conv_1", "run-2", "user", "¿Y las ventas?")

    assert store.deliver_if_current("conv_1", "run-2", "Respuesta", 0, reply_to=inbound)
    # Labelled a reply, but to a message of another run: the customer did not just write.
    assert store.deliver_if_current("conv_1", "run-3", "Seguimiento", 0, reply_to=earlier)
    assert store.deliver_if_current("conv_1", "run-4", "Novedad", 0, reply_to=None)

    assert store.deliveries("run-2") == Deliveries(replies=1, proactive=0)
    assert store.deliveries("run-3") == Deliveries(replies=0, proactive=1)
    assert store.deliveries("run-4") == Deliveries(replies=0, proactive=1)


def test_a_reply_in_a_run_that_handled_a_follow_up_is_a_contact(database: Database) -> None:
    store = database.for_tenant(DIRECTORY.authenticate("comercio_a", "operador_a"))
    store.open_conversation("conv_1")
    store.chat.record_followup("conv_1", "run-1", "digest", "CONTACTAR", ("FOLLOWUP_DUE",), None)
    inbound = store.append_message("conv_1", "run-1", "user", "Hola")

    assert store.deliver_if_current("conv_1", "run-1", "OK", 0, reply_to=inbound)

    assert store.deliveries("run-1") == Deliveries(replies=0, proactive=1)


@pytest.mark.parametrize("bad", ["", "conv 1", "conv/../x", "x" * 129])
def test_conversation_ids_use_a_closed_alphabet(database: Database, bad: str) -> None:
    store = database.for_tenant(DIRECTORY.authenticate("comercio_a", "operador_a"))

    with pytest.raises(ValueError, match="not a valid id"):
        store.open_conversation(bad)


def test_each_companys_alex_is_its_own_contact(database: Database) -> None:
    # kit/fixtures/tenants_ventas.json: two companies, each with a contact named Alex.
    store_a = database.for_tenant(DIRECTORY.authenticate("comercio_a", "operador_a"))
    store_b = database.for_tenant(DIRECTORY.authenticate("comercio_b", "operador_b"))
    store_a.record_contact("a_01", "Alex")
    store_b.record_contact("b_01", "Alex")
    store_b.record_contact("b_02", "Bruno")

    assert [c.contact_id for c in store_a.contacts(name="Alex")] == ["a_01"]
    assert [c.contact_id for c in store_b.contacts(name="Alex")] == ["b_01"]
    assert [c.contact_id for c in store_b.contacts()] == ["b_01", "b_02"]
    assert store_a.contacts(name="Bruno") == ()


def test_recording_a_contact_twice_keeps_one(database: Database) -> None:
    store = database.for_tenant(DIRECTORY.authenticate("comercio_a", "operador_a"))
    store.record_contact("a_01", "Alex")
    store.record_contact("a_01", "Alex")

    assert len(store.contacts()) == 1


# --------------------------------------------------------------------------- takeover versions


def _control_store(database: Database, mode: State, version: int) -> TenantStore:
    store = database.for_tenant(DIRECTORY.authenticate("comercio_a", "operador_a"))
    store.open_conversation("conv_1")
    store.chat.set_control("conv_1", mode, version)
    store.ledger.record_pending(
        PendingAction("op_1", "conv_1", "crear_reclamo", None, None, version, PendingStatus.PENDING)
    )
    return store


def _control(store: TenantStore) -> tuple[State, int, str | None]:
    conversation = store.conversation("conv_1")
    assert conversation is not None
    return conversation.mode, conversation.control_version, conversation.responsible


def _pending(store: TenantStore) -> list[PendingStatus]:
    return [action.status for action in store.ledger.open_actions("conv_1")]


def test_an_older_takeover_is_stale_and_changes_nothing(database: Database) -> None:
    store = _control_store(database, State.AUTO, 2)

    outcome = store.chat.apply_takeover("conv_1", 1, "operador_a")

    assert (outcome, outcome.applied) == (TakeoverOutcome.STALE, False)
    assert _control(store) == (State.AUTO, 2, None)
    assert _pending(store) == [PendingStatus.PENDING]
    assert store.chat.last_takeover_seq("conv_1") is None


def test_the_same_version_while_a_person_already_has_it_is_a_duplicate(
    database: Database,
) -> None:
    store = _control_store(database, State.HUMAN, 2)

    outcome = store.chat.apply_takeover("conv_1", 2, "operador_a")

    assert (outcome, outcome.applied) == (TakeoverOutcome.DUPLICATE, False)
    assert _control(store) == (State.HUMAN, 2, None)
    assert store.chat.last_takeover_seq("conv_1") is None


def test_the_same_version_while_the_agent_has_it_is_a_conflict_that_hands_it_over(
    database: Database,
) -> None:
    store = _control_store(database, State.AUTO, 2)

    outcome = store.chat.apply_takeover("conv_1", 2, "operador_a")

    assert (outcome, outcome.applied) == (TakeoverOutcome.CONFLICT, True)
    # A new version, so every checkpoint that saw version 2 notices the change.
    assert _control(store) == (State.HUMAN, 3, "operador_a")
    assert _pending(store) == [PendingStatus.SUSPENDED]
    assert store.chat.last_takeover_seq("conv_1") is not None
    assert not store.deliver_if_current(
        "conv_1", "run-1", "tarde", expected_version=2, reply_to=None
    )


@pytest.mark.parametrize("mode", [State.AUTO, State.HUMAN], ids=["first", "second_takeover"])
def test_a_newer_takeover_is_applied_at_its_version(database: Database, mode: State) -> None:
    store = _control_store(database, mode, 2)

    outcome = store.chat.apply_takeover("conv_1", 5, "operador_a")

    assert (outcome, outcome.applied) == (TakeoverOutcome.APPLIED, True)
    assert _control(store) == (State.HUMAN, 5, "operador_a")
    assert _pending(store) == [PendingStatus.SUSPENDED]

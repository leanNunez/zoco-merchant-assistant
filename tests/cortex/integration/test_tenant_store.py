from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex.identity import PrincipalDirectory, PrincipalRecord
from cortex.memory.database import Database
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

    assert store.deliver_if_current("conv_1", "run-1", "primera", expected_version=0)

    with database.transaction() as tx:  # an operator takes over: new version, HUMAN
        tx.execute(
            "UPDATE conversations SET mode = 'HUMAN', control_version = 1 "
            "WHERE tenant_id = 'comercio_a' AND conversation_id = 'conv_1'"
        )

    assert not store.deliver_if_current("conv_1", "run-2", "segunda", expected_version=0)
    assert store.sent_count("run-1") == 1
    assert store.sent_count("run-2") == 0
    conversation = store.conversation("conv_1")
    assert conversation is not None
    assert conversation.mode is State.HUMAN
    assert [m.content for m in store.recent_messages("conv_1", 10)] == ["primera"]


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

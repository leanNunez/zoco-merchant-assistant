"""Everything the harness stores for one company (brief section 4).

A `TenantStore` is built from an `AuthContext` and no method takes a company id: every query
filters by the company of that context. Reaching another company's rows is impossible by
construction, not by discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cortex.identifiers import EXTERNAL_ID
from cortex.identity import AuthContext
from cortex.memory.chat_records import ChatRecords
from cortex.memory.ledger import TenantLedger
from cortex.memory.runs import RunJournal
from cortex.memory.traces import TraceStore
from merchant_assistant.control.states import State

if TYPE_CHECKING:
    from cortex.memory.database import Database

MessageRole = Literal["user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ConversationState:
    conversation_id: str
    mode: State
    control_version: int
    responsible: str | None


@dataclass(frozen=True, slots=True)
class StoredMessage:
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class Deliveries:
    """What one run delivered to the customer, as the outbox proves it."""

    replies: int  # answers to an inbound message of the same run
    proactive: int  # anything else: the system wrote to the customer on its own initiative


@dataclass(frozen=True, slots=True)
class Contact:
    contact_id: str
    name: str


def check_external_id(value: str) -> str:
    if EXTERNAL_ID.fullmatch(value) is None:
        raise ValueError(f"not a valid id: {value[:40]!r}")
    return value


class TenantStore:
    def __init__(self, database: Database, auth: AuthContext) -> None:
        self._db = database
        self.tenant_id = auth.tenant_id
        self.ledger = TenantLedger(database, auth)
        self.runs = RunJournal(database, auth)
        self.chat = ChatRecords(database, auth)
        self.traces = TraceStore(database, auth)

    def open_conversation(self, conversation_id: str) -> ConversationState:
        """The conversation of this company with that id; created in AUTO the first time."""
        check_external_id(conversation_id)
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO conversations (tenant_id, conversation_id, created_at) "
                "VALUES (?, ?, ?)",
                (self.tenant_id, conversation_id, self._db.now()),
            )
        state = self.conversation(conversation_id)
        if state is None:
            raise RuntimeError("a conversation vanished right after being opened")
        return state

    def conversation(self, conversation_id: str) -> ConversationState | None:
        with self._db.reading() as db:
            row = db.execute(
                "SELECT mode, control_version, responsible FROM conversations "
                "WHERE tenant_id = ? AND conversation_id = ?",
                (self.tenant_id, conversation_id),
            ).fetchone()
        if row is None:
            return None
        return ConversationState(conversation_id, State(row[0]), row[1], row[2])

    def record_contact(self, contact_id: str, name: str) -> None:
        """A contact of this company, as its system of record lists it. Recording it again
        changes nothing."""
        check_external_id(contact_id)
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO contacts (tenant_id, contact_id, name) VALUES (?, ?, ?)",
                (self.tenant_id, contact_id, name),
            )

    def contacts(self, name: str | None = None) -> tuple[Contact, ...]:
        """This company's contacts by id, only those called `name` when it is given. Another
        company's contact with the same name is never among them."""
        with self._db.reading() as db:
            rows = db.execute(
                "SELECT contact_id, name FROM contacts "
                "WHERE tenant_id = ? AND (? IS NULL OR name = ?) ORDER BY contact_id",
                (self.tenant_id, name, name),
            ).fetchall()
        return tuple(Contact(contact_id, contact_name) for contact_id, contact_name in rows)

    def append_message(
        self, conversation_id: str, run_id: str, role: MessageRole, content: str
    ) -> int:
        """Store a message and return its id (a reply names the message it answers by it)."""
        with self._db.transaction() as tx:
            cursor = tx.execute(
                "INSERT INTO messages (tenant_id, conversation_id, run_id, role, content, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.tenant_id, conversation_id, run_id, role, content, self._db.now()),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("a stored message has no id")
        return cursor.lastrowid

    def recent_messages(self, conversation_id: str, limit: int) -> tuple[StoredMessage, ...]:
        """The last `limit` messages, oldest first: the bounded history the model receives."""
        with self._db.reading() as db:
            rows = db.execute(
                "SELECT role, content FROM messages WHERE tenant_id = ? AND conversation_id = ? "
                "ORDER BY message_id DESC LIMIT ?",
                (self.tenant_id, conversation_id, limit),
            ).fetchall()
        return tuple(StoredMessage(role, content) for role, content in reversed(rows))

    def deliver_if_current(
        self,
        conversation_id: str,
        run_id: str,
        text: str,
        expected_version: int,
        *,
        reply_to: int | None,
    ) -> bool:
        """Send `text` only if the agent still controls the conversation at that version.

        `reply_to` is the inbound message the text answers; None means the system writes to the
        customer on its own initiative. The check and the write happen in one write transaction,
        so an operator's takeover either lands before (the reply is suppressed) or after (it was
        already sent).
        """
        with self._db.transaction() as tx:
            row = tx.execute(
                "SELECT mode, control_version FROM conversations "
                "WHERE tenant_id = ? AND conversation_id = ?",
                (self.tenant_id, conversation_id),
            ).fetchone()
            current = row is not None and row[0] == State.AUTO and row[1] == expected_version
            tx.execute(
                "INSERT INTO outbox (tenant_id, conversation_id, run_id, content, status, kind, "
                "reply_to, control_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.tenant_id,
                    conversation_id,
                    run_id,
                    text,
                    "SENT" if current else "SUPPRESSED",
                    "proactive" if reply_to is None else "reply",
                    reply_to,
                    expected_version,
                    self._db.now(),
                ),
            )
            if current:
                tx.execute(
                    "INSERT INTO messages (tenant_id, conversation_id, run_id, role, content, "
                    "created_at) VALUES (?, ?, ?, 'assistant', ?, ?)",
                    (self.tenant_id, conversation_id, run_id, text, self._db.now()),
                )
                tx.execute(
                    "INSERT INTO journal (tenant_id, conversation_id, kind, reference, "
                    "created_at) VALUES (?, ?, 'delivery', ?, ?)",
                    (self.tenant_id, conversation_id, run_id, self._db.now()),
                )
        return current

    def last_message(self, conversation_id: str, role: MessageRole) -> str | None:
        with self._db.reading() as db:
            row = db.execute(
                "SELECT content FROM messages "
                "WHERE tenant_id = ? AND conversation_id = ? AND role = ? "
                "ORDER BY message_id DESC LIMIT 1",
                (self.tenant_id, conversation_id, role),
            ).fetchone()
        return None if row is None else str(row[0])

    def last_outbox(
        self, conversation_id: str, status: Literal["SENT", "SUPPRESSED"]
    ) -> str | None:
        with self._db.reading() as db:
            row = db.execute(
                "SELECT content FROM outbox "
                "WHERE tenant_id = ? AND conversation_id = ? AND status = ? "
                "ORDER BY outbox_id DESC LIMIT 1",
                (self.tenant_id, conversation_id, status),
            ).fetchone()
        return None if row is None else str(row[0])

    def deliveries(self, run_id: str) -> Deliveries:
        """The messages the run sent, split by what they were.

        A reply needs evidence: it names an inbound message of the same company, conversation and
        run, and the run did not handle a follow-up (an event-triggered run answers nobody).
        Anything else sent counts as contacting the customer, so a mislabelled message is never
        hidden among the replies.
        """
        with self._db.reading() as db:
            rows = db.execute(
                "SELECT o.kind = 'reply' AND EXISTS ("
                "  SELECT 1 FROM messages m WHERE m.tenant_id = o.tenant_id "
                "  AND m.message_id = o.reply_to AND m.conversation_id = o.conversation_id "
                "  AND m.run_id = o.run_id AND m.role = 'user'"
                ") AND NOT EXISTS ("
                "  SELECT 1 FROM followup_decisions f WHERE f.tenant_id = o.tenant_id "
                "  AND f.run_id = o.run_id"
                ") FROM outbox o WHERE o.tenant_id = ? AND o.run_id = ? AND o.status = 'SENT'",
                (self.tenant_id, run_id),
            ).fetchall()
        replies = sum(1 for (is_reply,) in rows if is_reply)
        return Deliveries(replies=replies, proactive=len(rows) - replies)

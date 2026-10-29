"""The continuity card: what an operator needs when taking a conversation over (brief section 6).

It is computed from what is stored, never written by the model: the customer's last query, the
last tool and its outcome, every pending action with its exact values, the writes whose outcome
is still unknown, what is owed, the last reply sent and the last one held back, and who is
responsible now.
"""

from __future__ import annotations

import json

from cortex.memory.tenant_store import TenantStore

QUERY_PREVIEW = 200


def continuity_card(store: TenantStore, conversation_id: str) -> dict[str, object]:
    conversation = store.conversation(conversation_id)
    last_query = store.last_message(conversation_id, "user")
    return {
        "mode": conversation.mode.value if conversation else None,
        "control_version": conversation.control_version if conversation else None,
        "responsible": conversation.responsible if conversation else None,
        "last_query": last_query[:QUERY_PREVIEW] if last_query else None,
        "last_tool": _last_tool(store, conversation_id),
        "pending_actions": [action.view() for action in store.ledger.open_actions(conversation_id)],
        "uncertain_operations": list(store.ledger.uncertain_operations()),
        "commitments": [
            {"kind": c.kind, "responsible": c.responsible, "due_date": c.due_date}
            for c in store.chat.open_commitments(conversation_id)
        ],
        "last_reply_sent": store.last_outbox(conversation_id, "SENT"),
        "last_reply_suppressed": store.last_outbox(conversation_id, "SUPPRESSED"),
    }


def _last_tool(store: TenantStore, conversation_id: str) -> dict[str, object] | None:
    envelope = store.last_message(conversation_id, "tool")
    if envelope is None:
        return None
    data = json.loads(envelope)
    return {"tool": data.get("tool"), "status": data.get("status")}

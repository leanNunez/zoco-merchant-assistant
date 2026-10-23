"""Why the retrieval inputs were refused.

Each error has a stable `code` and a safe `user_message`. The `detail` names the file,
line or key for the logs only: it must not reach the customer or the LLM, because it can
quote corpus text.
"""

from __future__ import annotations

from typing import ClassVar

from merchant_assistant.shared.errors import DomainError


class KnowledgeError(DomainError):
    """Base of every refusal raised by the retrieval domain."""


class DocumentFormatError(KnowledgeError):
    """A document file does not follow the front matter and heading format."""

    code: ClassVar[str] = "invalid_document"
    user_message: ClassVar[str] = "A document could not be read."


class QuestionFormatError(KnowledgeError):
    """A line of the questions file does not follow the JSONL format."""

    code: ClassVar[str] = "invalid_question"
    user_message: ClassVar[str] = "A question could not be read."


class InvalidEmbeddingError(KnowledgeError):
    """A vector cannot be searched: wrong shape, not finite, or no direction."""

    code: ClassVar[str] = "invalid_embedding"
    user_message: ClassVar[str] = "The embeddings returned by the provider are not usable."

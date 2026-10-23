"""The OpenAI adapter against a hand-written fake client shaped like the SDK's."""

from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx2
import numpy as np
import openai
import pytest

from merchant_assistant.knowledge.adapters.openai_embedder import OpenAIEmbedder
from merchant_assistant.knowledge.errors import EmbeddingsUnavailableError, InvalidEmbeddingError
from merchant_assistant.shared.errors import DomainError

REQUEST = httpx2.Request("POST", "https://example.invalid/v1/embeddings")


@dataclass(frozen=True)
class Item:
    embedding: list[float]
    index: int


@dataclass(frozen=True)
class Response:
    data: list[Item]


@dataclass
class FakeEmbeddings:
    """Embeds "t<n>" as [n, 1]; can reverse the order of `data` or raise instead."""

    reverse: bool = False
    error: Exception | None = None
    vectors: dict[str, list[float]] = field(default_factory=dict)
    calls: list[tuple[list[str], str]] = field(default_factory=list)

    def create(self, *, input: list[str], model: str) -> Response:
        self.calls.append((list(input), model))
        if self.error is not None:
            raise self.error
        items = [
            Item(self.vectors.get(text, [float(text[1:]), 1.0]), index)
            for index, text in enumerate(input)
        ]
        return Response(items[::-1] if self.reverse else items)


@dataclass
class FakeClient:
    embeddings: FakeEmbeddings = field(default_factory=FakeEmbeddings)


def texts(count: int) -> Sequence[str]:
    return [f"t{n}" for n in range(count)]


def test_rows_come_back_in_input_order_as_float64() -> None:
    vectors = OpenAIEmbedder(FakeClient(), "m").embed(["t3", "t1", "t2"])

    assert vectors.dtype == np.float64
    assert vectors.tolist() == [[3.0, 1.0], [1.0, 1.0], [2.0, 1.0]]


def test_rows_are_paired_with_their_text_by_index_even_when_the_api_reorders_them() -> None:
    client = FakeClient(FakeEmbeddings(reverse=True))

    vectors = OpenAIEmbedder(client, "m").embed(["t3", "t1", "t2"])

    assert vectors.tolist() == [[3.0, 1.0], [1.0, 1.0], [2.0, 1.0]]


def test_texts_are_sent_in_batches_with_the_configured_model() -> None:
    client = FakeClient()
    embedder = OpenAIEmbedder(client, "text-embedding-3-small", batch_size=2)

    vectors = embedder.embed(texts(5))

    assert client.embeddings.calls == [
        (["t0", "t1"], "text-embedding-3-small"),
        (["t2", "t3"], "text-embedding-3-small"),
        (["t4"], "text-embedding-3-small"),
    ]
    assert vectors[:, 0].tolist() == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert embedder.requests == 3


def test_no_texts_make_no_request() -> None:
    client = FakeClient()

    assert OpenAIEmbedder(client, "m").embed([]).shape == (0, 0)
    assert client.embeddings.calls == []


def test_the_model_is_the_configured_one() -> None:
    assert OpenAIEmbedder(FakeClient(), "text-embedding-3-small").model == "text-embedding-3-small"


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
        openai.RateLimitError(
            "Rate limit reached", response=httpx2.Response(429, request=REQUEST), body=None
        ),
        openai.AuthenticationError(
            "Incorrect API key", response=httpx2.Response(401, request=REQUEST), body=None
        ),
        openai.NotFoundError(
            "model not found", response=httpx2.Response(404, request=REQUEST), body=None
        ),
        openai.BadRequestError(
            "input too long", response=httpx2.Response(400, request=REQUEST), body=None
        ),
    ],
    ids=lambda error: type(error).__name__,
)
def test_every_sdk_failure_becomes_a_safe_domain_error(error: openai.OpenAIError) -> None:
    client = FakeClient(FakeEmbeddings(error=error))

    with pytest.raises(EmbeddingsUnavailableError) as refused:
        OpenAIEmbedder(client, "m").embed(["t1"])

    assert isinstance(refused.value, DomainError)
    assert refused.value.public() == {
        "code": "embeddings_unavailable",
        "message": "The search service is temporarily unavailable.",
    }
    assert refused.value.detail == f"{type(error).__name__}: {error}"


def test_the_sdk_message_stays_in_the_detail_and_out_of_the_public_view() -> None:
    error = openai.AuthenticationError(
        "Incorrect API key provided: sk-abc",
        response=httpx2.Response(401, request=REQUEST),
        body=None,
    )
    client = FakeClient(FakeEmbeddings(error=error))

    with pytest.raises(EmbeddingsUnavailableError) as refused:
        OpenAIEmbedder(client, "m").embed(["t1"])

    assert refused.value.detail == "AuthenticationError: Incorrect API key provided: sk-abc"
    assert "sk-abc" not in str(refused.value.public())


def test_an_error_that_is_not_from_the_sdk_is_not_disguised_as_unavailability() -> None:
    client = FakeClient(FakeEmbeddings(error=KeyError("bug")))

    with pytest.raises(KeyError):
        OpenAIEmbedder(client, "m").embed(["t1"])


def test_rows_of_different_dimensions_are_refused() -> None:
    client = FakeClient(FakeEmbeddings(vectors={"t1": [1.0, 2.0, 3.0]}))

    with pytest.raises(InvalidEmbeddingError) as refused:
        OpenAIEmbedder(client, "m").embed(["t0", "t1"])

    assert refused.value.detail == "rows of different dimensions: [2, 3]"


def test_rows_of_different_dimensions_across_batches_are_refused() -> None:
    client = FakeClient(FakeEmbeddings(vectors={"t1": [1.0, 2.0, 3.0]}))

    with pytest.raises(InvalidEmbeddingError, match="different dimensions"):
        OpenAIEmbedder(client, "m", batch_size=1).embed(["t0", "t1"])


@pytest.mark.parametrize(
    ("vector", "fragment"),
    [
        ([float("nan"), 1.0], "NaN or infinite"),
        ([float("inf"), 1.0], "NaN or infinite"),
        ([], "empty vectors"),
    ],
)
def test_unusable_vectors_are_refused(vector: list[float], fragment: str) -> None:
    client = FakeClient(FakeEmbeddings(vectors={"t0": vector}))

    with pytest.raises(InvalidEmbeddingError, match=fragment):
        OpenAIEmbedder(client, "m").embed(["t0"])


def test_a_response_that_skips_an_index_is_refused() -> None:
    class SkippingEmbeddings(FakeEmbeddings):
        def create(self, *, input: list[str], model: str) -> Response:
            return Response([Item([1.0, 1.0], 0), Item([1.0, 1.0], 2)])

    with pytest.raises(InvalidEmbeddingError) as refused:
        OpenAIEmbedder(FakeClient(SkippingEmbeddings()), "m").embed(["t0", "t1"])

    assert refused.value.detail == "asked for 2 vectors, got indexes [0, 2]"


def test_a_batch_size_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        OpenAIEmbedder(FakeClient(), "m", batch_size=0)

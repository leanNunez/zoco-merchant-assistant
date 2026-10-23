import pytest

from merchant_assistant.knowledge.adapters.missing_key import MissingKeyEmbedder
from merchant_assistant.knowledge.errors import EmbeddingsUnavailableError


def test_embedding_without_a_key_fails_and_says_how_to_fix_it() -> None:
    with pytest.raises(EmbeddingsUnavailableError) as refused:
        MissingKeyEmbedder("text-embedding-3-small").embed(["a", "b"])

    assert refused.value.detail == (
        "2 texts are not in the embeddings cache and no API key is configured: "
        "set OPENAI_API_KEY in .env"
    )
    assert refused.value.code == "embeddings_unavailable"


def test_it_reports_the_configured_model_so_cache_keys_still_match() -> None:
    assert MissingKeyEmbedder("text-embedding-3-small").model == "text-embedding-3-small"

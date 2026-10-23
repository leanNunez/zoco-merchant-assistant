"""The real provider. Deselected by default; run on purpose with `pytest -m network`."""

import numpy as np
import openai
import pytest

from merchant_assistant.knowledge.adapters.openai_embedder import OpenAIEmbedder
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.network


def test_the_real_api_embeds_two_short_texts_into_finite_rows_of_one_dimension() -> None:
    settings = Settings()
    if settings.openai_api_key is None:
        pytest.skip("OPENAI_API_KEY is not configured")
    client = openai.OpenAI(
        api_key=settings.openai_api_key.get_secret_value(), timeout=30, max_retries=0
    )

    vectors = OpenAIEmbedder(client, settings.embeddings_model).embed(["hola", "chau"])

    assert vectors.shape[0] == 2
    assert vectors.shape[1] > 0
    assert np.isfinite(vectors).all()

"""BM25 over chunks, written from the formula.

    idf(t)      = ln(1 + (N - n_t + 0.5) / (n_t + 0.5))
    score(q, d) = sum over DISTINCT tokens t of q of
                  idf(t) * tf(t, d) * (k1 + 1) / (tf(t, d) + k1 * (1 - b + b * dl(d) / avgdl))

The Lucene form of the IDF is used because the classic one turns negative for a term that
appears in more than half of the chunks, which would make a matching word lower a score.
The corpus here is tiny, so that case is the normal one and not a corner.

Query tokens count once: repeating a word in the question does not make it more important
than the corpus says it is.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

from merchant_assistant.knowledge.chunking import Chunk
from merchant_assistant.knowledge.ranking import ScoredChunk
from merchant_assistant.knowledge.text import Tokenizer


class BM25Index:
    def __init__(
        self,
        chunks: Sequence[Chunk],
        tokenizer: Tokenizer,
        *,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        # Written so that NaN fails too: every comparison with NaN is false.
        if not k1 >= 0:
            raise ValueError("k1 must be >= 0")
        if not 0 <= b <= 1:
            raise ValueError("b must be between 0 and 1")
        self._chunks = tuple(chunks)
        # The index owns its tokenizer and applies it to queries, so the two sides cannot
        # drift apart.
        self._tokenizer = tokenizer
        self._k1 = k1
        self._b = b

        lengths: list[int] = []
        postings: dict[str, list[tuple[int, int]]] = {}
        for position, chunk in enumerate(self._chunks):
            counts = Counter(tokenizer(chunk.index_text))
            lengths.append(sum(counts.values()))
            for token, frequency in counts.items():
                postings.setdefault(token, []).append((position, frequency))
        self._lengths = lengths
        self._postings = postings
        # Only read when some chunk has a token, so it is never zero when it is used.
        self._average_length = sum(lengths) / len(lengths) if lengths else 0.0
        total = len(self._chunks)
        self._idf = {
            token: math.log(1 + (total - len(entries) + 0.5) / (len(entries) + 0.5))
            for token, entries in postings.items()
        }

    def scores(self, query: str) -> list[float]:
        """One score per chunk, aligned with the corpus order."""
        scores = [0.0] * len(self._chunks)
        for token in dict.fromkeys(self._tokenizer(query)):
            for position, frequency in self._postings.get(token, ()):
                length_ratio = self._lengths[position] / self._average_length
                norm = self._k1 * (1 - self._b + self._b * length_ratio)
                scores[position] += (
                    self._idf[token] * frequency * (self._k1 + 1) / (frequency + norm)
                )
        return scores

    def search(self, query: str, k: int) -> list[ScoredChunk]:
        """The best `k` chunks that share at least one token with the query.

        Chunks with a score of zero are left out instead of padding the result: a lexical
        miss must look like a miss to whoever decides whether the system knows the answer.
        """
        if k < 1:
            raise ValueError("k must be >= 1")
        scores = self.scores(query)
        # Sorting on (-score, position) makes ties deterministic: the earlier chunk wins.
        order = sorted(
            (position for position, score in enumerate(scores) if score > 0),
            key=lambda position: (-scores[position], position),
        )
        return [
            ScoredChunk(self._chunks[position].id, self._chunks[position].doc_id, scores[position])
            for position in order[:k]
        ]

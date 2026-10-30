"""OpenAI-compatible embeddings; optional and never used on realtime audio."""

import math
from whale_core.plugins.http_provider import JsonProvider, ProviderError


class OpenAIEmbeddingProvider(JsonProvider):
    def embed(self, texts):
        if not texts or len(texts) > 512 or any(not isinstance(x, str) for x in texts):
            raise ValueError("expected 1..512 texts")
        data = self.request(
            "/embeddings", {"model": self.model, "input": [x[:4000] for x in texts]}
        )
        try:
            rows = sorted(data["data"], key=lambda x: x["index"])
            if [x["index"] for x in rows] != list(range(len(texts))):
                raise ValueError()
            vectors = [x["embedding"] for x in rows]
            dim = len(vectors[0])
            if not dim or any(
                len(v) != dim
                or any(
                    isinstance(n, bool)
                    or not isinstance(n, (int, float))
                    or not math.isfinite(n)
                    for n in v
                )
                for v in vectors
            ):
                raise ValueError()
            return vectors
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("bad_response") from exc

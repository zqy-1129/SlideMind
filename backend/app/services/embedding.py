import hashlib
import math

from app.core.config import settings


def embed_text(text: str) -> list[float]:
    """Deterministic placeholder embedding; replace with a real model in production."""
    vector = [0.0] * settings.embedding_dim
    tokens = [token.strip().lower() for token in text.split() if token.strip()]
    for token in tokens or [text[:32]]:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "little") % settings.embedding_dim
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


"""Embeddings lokal auf dem Server (Extra 'embed'), Dimension 1024 passend zum Schema."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

DIM = 1024


class Embedder(Protocol):
    name: str

    def embed(self, texte: list[str], art: str = "passage") -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Standard: intfloat/multilingual-e5-large (mehrsprachig, 1024 Dimensionen)."""

    def __init__(self, model: str = "intfloat/multilingual-e5-large", device: str | None = None):
        """device: None = automatisch (GPU falls frei), "cpu" oder "cuda"."""
        from sentence_transformers import SentenceTransformer
        self.name = model
        self._m = SentenceTransformer(model, device=device or None)
        if self._m.get_sentence_embedding_dimension() != DIM:
            raise ValueError(f"{model} hat nicht {DIM} Dimensionen")

    def embed(self, texte: list[str], art: str = "passage") -> list[list[float]]:
        prefix = "query: " if art == "query" else "passage: "   # Konvention der E5-Modelle
        vecs = self._m.encode([prefix + t for t in texte], normalize_embeddings=True, batch_size=16)
        return [v.tolist() for v in vecs]


class LazyEmbedder:
    """Lädt das eigentliche Modell erst beim ersten Aufruf. Der MCP-Server meldet sich so sofort beim
    Client (kein Timeout beim Start); die erste Suche dauert dafür einige Sekunden länger."""

    def __init__(self, fabrik):
        self._fabrik = fabrik
        self._e = None
        self.name = "lazy"

    def embed(self, texte: list[str], art: str = "passage") -> list[list[float]]:
        if self._e is None:
            self._e = self._fabrik()
            self.name = self._e.name
        return self._e.embed(texte, art)


class HashEmbedder:
    """Deterministischer Ersatz für Tests und Trockenläufe (keine Semantik, nur Wortüberlappung)."""

    name = "hash-embedder"

    def embed(self, texte: list[str], art: str = "passage") -> list[list[float]]:
        out = []
        for t in texte:
            v = [0.0] * DIM
            for w in re.findall(r"\w+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def als_vector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

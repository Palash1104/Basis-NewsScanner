"""Local sentence embeddings for grouping (sentence-transformers on CPU; no API calls, so no
LLM quota). The model is downloaded once from Hugging Face and cached; later loads are offline.
"""

import logging
import os
from collections.abc import Sequence
from functools import lru_cache
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

# Quieter model loading: no progress bars or Windows symlink warnings in run output.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return an (n, d) float32 array of L2-normalized embeddings."""
        ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # heavy import: only when used

        self.name = model_name
        try:  # use the local cache without contacting Hugging Face
            self._model = SentenceTransformer(model_name, device="cpu", local_files_only=True)
        except Exception:
            log.info("embedding model %s not cached yet; downloading it once", model_name)
            self._model = SentenceTransformer(model_name, device="cpu")
        self.dimension = int(self._model.get_embedding_dimension() or 0)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        vectors = self._model.encode(
            list(texts),
            batch_size=64,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


@lru_cache(maxsize=2)
def _load(model_name: str) -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder(model_name)


def load_embedder(model_name: str) -> tuple[Embedder | None, str | None]:
    """The cached embedder, or (None, reason) if the model can't be loaded. Callers then fall
    back to the title matcher."""
    try:
        return _load(model_name), None
    except Exception as exc:  # missing download, no network on first use, broken install
        log.warning("embedding model %s unavailable: %s", model_name, exc)
        return None, f"{type(exc).__name__}: {exc}"


def article_text(title: str, snippet: str) -> str:
    """What gets embedded for an article: headline plus snippet."""
    return f"{title}. {snippet}" if snippet else title

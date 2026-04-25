import asyncio

from sentence_transformers import SentenceTransformer

from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_model: SentenceTransformer | None = None
_MODEL_NAME = "all-MiniLM-L6-v2"


def get_embedding_model() -> SentenceTransformer:
    """Lazy singleton — loads model once per process on first call."""
    global _model
    if _model is None:
        LOGGER.info("Loading embedding model", extra={"model": _MODEL_NAME})
        _model = SentenceTransformer(_MODEL_NAME)
        LOGGER.info("Embedding model loaded", extra={"model": _MODEL_NAME, "dim": 384})
    return _model


async def embed_text(text: str) -> list[float]:
    """Embed text to 384-dim float list. Wraps blocking call in thread executor."""
    model = get_embedding_model()
    loop = asyncio.get_event_loop()
    embedding = await loop.run_in_executor(None, lambda: model.encode(text, normalize_embeddings=True))
    return embedding.tolist()


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch embed multiple texts. More efficient than calling embed_text N times."""
    model = get_embedding_model()
    loop = asyncio.get_event_loop()
    embeddings = await loop.run_in_executor(
        None, lambda: model.encode(texts, normalize_embeddings=True, batch_size=32)
    )
    return [e.tolist() for e in embeddings]

import asyncio
import tempfile
from pathlib import Path

from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_converter = None


def get_document_converter():
    """Lazy singleton DocumentConverter — loads ML model once per process."""
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter
        LOGGER.info("Loading docling DocumentConverter")
        _converter = DocumentConverter()
        LOGGER.info("docling DocumentConverter ready")
    return _converter


_SUPPORTED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


async def parse_document(raw_bytes: bytes, mime_type: str) -> str:
    """Convert document bytes to Markdown string. Runs docling in thread executor."""
    if mime_type in ("text/markdown", "text/plain"):
        # No conversion needed — return decoded text directly
        return raw_bytes.decode("utf-8", errors="replace")

    ext = _SUPPORTED_MIME_TYPES.get(mime_type)
    if ext is None:
        LOGGER.warning(
            "Unsupported mime_type — attempting PDF parse",
            extra={"mime_type": mime_type},
        )
        ext = ".pdf"

    def _convert() -> str:
        converter = get_document_converter()
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = Path(tmp.name)
        try:
            result = converter.convert(str(tmp_path))
            return result.document.export_to_markdown()
        finally:
            tmp_path.unlink(missing_ok=True)

    loop = asyncio.get_event_loop()
    markdown = await loop.run_in_executor(None, _convert)
    LOGGER.info(
        "Document parsed to markdown",
        extra={"mime_type": mime_type, "markdown_len": len(markdown)},
    )
    return markdown

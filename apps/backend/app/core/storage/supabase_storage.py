from __future__ import annotations

from urllib.parse import quote

import httpx

from app.core.config import settings
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class SupabaseStorageClient:
    """Minimal Supabase Storage client for private bucket upload/download."""

    def __init__(self) -> None:
        if not settings.supabase.url:
            raise ValueError("SUPABASE_URL is required for storage operations")
        if not settings.supabase.service_role_key:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY is required for storage operations")
        self._base_url = settings.supabase.url.rstrip("/")
        self._service_role_key = settings.supabase.service_role_key
        self._client = httpx.AsyncClient(timeout=60.0)

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._service_role_key}",
            "apikey": self._service_role_key,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    async def upload_bytes(
        self,
        *,
        bucket: str,
        path: str,
        data: bytes,
        content_type: str,
    ) -> None:
        encoded_path = quote(path, safe="/")
        url = f"{self._base_url}/storage/v1/object/{bucket}/{encoded_path}"
        resp = await self._client.post(
            url,
            content=data,
            headers={**self._headers(content_type), "x-upsert": "true"},
        )
        resp.raise_for_status()
        LOGGER.info("Uploaded object to Supabase storage", extra={"bucket": bucket, "path": path})

    async def download_bytes(self, *, bucket: str, path: str) -> bytes:
        encoded_path = quote(path, safe="/")
        url = f"{self._base_url}/storage/v1/object/{bucket}/{encoded_path}"
        resp = await self._client.get(url, headers=self._headers())
        resp.raise_for_status()
        return resp.content

    async def close(self) -> None:
        await self._client.aclose()


_singleton: SupabaseStorageClient | None = None


def get_supabase_storage_client() -> SupabaseStorageClient:
    global _singleton
    if _singleton is None:
        _singleton = SupabaseStorageClient()
    return _singleton


async def close_supabase_storage_client() -> None:
    global _singleton
    if _singleton is not None:
        await _singleton.close()
        _singleton = None

"""JWKS (JSON Web Key Set) service for Supabase JWT verification.

This module handles fetching, caching, and managing Supabase's public keys
used for JWT signature verification.
"""

import asyncio
import time

import aiohttp
from pydantic import BaseModel

from app.core.config import settings
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class JWKKey(BaseModel):
    """JSON Web Key model."""

    kid: str
    kty: str
    use: str
    crv: str | None = None
    ext: bool | None = None
    x: str | None = None
    y: str | None = None
    n: str | None = None
    e: str | None = None
    key_ops: list[str]
    alg: str


class JWKSResponse(BaseModel):
    """JWKS response model."""

    keys: list[JWKKey]


class JWKSService:
    """Service for fetching and caching Supabase JWKS keys.

    This service handles:
    - Fetching JWKS from Supabase
    - In-memory caching with TTL
    - Thread-safe operations
    """

    def __init__(
        self,
        supabase_url: str,
        cache_ttl: int = 3600,
        timeout: int = 30,
    ):
        """Initialize JWKS service.

        Args:
            supabase_url: Supabase project URL
            cache_ttl: Cache time-to-live in seconds
            timeout: HTTP request timeout in seconds
        """
        self.supabase_url = supabase_url.rstrip("/")
        self.jwks_url = f"{self.supabase_url}/auth/v1/.well-known/jwks.json"
        self.cache_ttl = cache_ttl
        self.timeout = timeout

        self._keys_cache: dict[str, JWKKey] | None = None
        self._cache_timestamp: float | None = None
        self._lock = asyncio.Lock()

        LOGGER.info(f"JWKS service initialized for {self.supabase_url}")

    async def get_keys(self) -> dict[str, JWKKey]:
        """Get JWKS keys, using cache if valid.

        Returns:
            Dictionary mapping key IDs to JWK keys

        Raises:
            RuntimeError: If keys cannot be fetched
        """
        async with self._lock:
            if self._is_cache_valid():
                LOGGER.debug("Using cached JWKS keys")
                return self._keys_cache.copy()

            LOGGER.info("Fetching fresh JWKS keys from Supabase")
            keys = await self._fetch_keys()
            self._update_cache(keys)

            return keys.copy()

    async def get_key(self, kid: str) -> JWKKey | None:
        """Get a specific key by key ID.

        Args:
            kid: Key ID to look up

        Returns:
            JWK key if found, None otherwise
        """
        keys = await self.get_keys()
        return keys.get(kid)

    def _is_cache_valid(self) -> bool:
        """Check if current cache is still valid."""
        if self._keys_cache is None or self._cache_timestamp is None:
            return False

        elapsed = time.time() - self._cache_timestamp
        return elapsed < self.cache_ttl

    async def _fetch_keys(self) -> dict[str, JWKKey]:
        """Fetch JWKS keys from Supabase."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as session:
                async with session.get(self.jwks_url) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            f"JWKS endpoint returned {response.status}: {await response.text()}"
                        )

                    data = await response.json()
                    jwks_response = JWKSResponse(**data)
                    keys = {key.kid: key for key in jwks_response.keys}

                    LOGGER.info(f"Successfully fetched {len(keys)} JWKS keys")
                    return keys

        except aiohttp.ClientError as e:
            LOGGER.error(f"Network error fetching JWKS: {e}")
            raise RuntimeError(f"Failed to fetch JWKS keys: {e}") from e
        except Exception as e:
            LOGGER.error(f"Error parsing JWKS response: {e}")
            raise RuntimeError(f"Invalid JWKS response: {e}") from e

    def _update_cache(self, keys: dict[str, JWKKey]) -> None:
        """Update the internal cache."""
        self._keys_cache = keys.copy()
        self._cache_timestamp = time.time()
        LOGGER.debug(f"Updated JWKS cache with {len(keys)} keys")


# Global JWKS service instance
jwks_service = JWKSService(
    supabase_url=settings.supabase_url,
    cache_ttl=settings.supabase_jwks_cache_ttl,
)

from abc import ABC, abstractmethod
from typing import Any
from app.core.exceptions import AppError
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class BaseService(ABC):
    def __init__(self, repository=None):
        self.repository = repository
        self.logger = LOGGER

    async def execute(self, *args, **kwargs) -> Any:
        try:
            self.validate(*args, **kwargs)
            return await self.run(*args, **kwargs)
        except AppError:
            raise
        except Exception as e:
            self.logger.error(
                f"Service execution failed: {e}",
                exc_info=True,
                extra={"service": self.__class__.__name__},
            )
            raise AppError(f"Service execution failed: {e}", original_error=e)

    def validate(self, *args, **kwargs):
        pass

    @abstractmethod
    async def run(self, *args, **kwargs) -> Any:
        ...

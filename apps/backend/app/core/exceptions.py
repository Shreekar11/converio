class AppError(Exception):
    def __init__(self, message: str, original_error: Exception = None):
        super().__init__(message)
        self.original_error = original_error


class ValidationError(AppError):
    pass


class DatabaseError(AppError):
    pass


class NotFoundError(AppError):
    pass


class AuthenticationError(AppError):
    pass

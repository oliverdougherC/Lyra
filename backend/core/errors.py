"""Domain errors carrying a user-facing message and an HTTP status.

A single FastAPI handler maps these to responses, so no route builds its own error
shape. Messages are written for the user: they never contain a filesystem path, an
endpoint URL, or the tutor API key.
"""


class LyraError(Exception):
    """Base domain error. Defaults to 400, the common case for bad input."""

    status: int = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(LyraError):
    status = 404


class ConflictError(LyraError):
    status = 409


class UpstreamError(LyraError):
    """The user's tutor endpoint failed or misbehaved."""

    status = 502


class ConfigurationError(LyraError):
    """Something the user must configure is missing or unusable."""

    status = 400

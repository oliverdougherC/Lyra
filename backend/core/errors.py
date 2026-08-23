"""Domain errors carrying a user-facing message and an HTTP status.

A single FastAPI handler maps these to responses, so no route builds its own error
shape. Messages are written for the user: they never contain a filesystem path, an
endpoint URL, or the tutor API key.
"""


class LyraError(Exception):
    """Base domain error. Defaults to 400, the common case for bad input.

    `extra` is merged into the JSON error body alongside `detail`, for the rare error that
    the client must act on structurally rather than only show. It never carries a
    filesystem path, an endpoint URL, or the tutor API key, the same rule as `message`.
    """

    status: int = 400

    def __init__(self, message: str, *, extra: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra


class NotFoundError(LyraError):
    status = 404


class ConflictError(LyraError):
    status = 409


class StaleContentError(ConflictError):
    """A body write named a version the part has already moved past (PLA-289).

    Carries the authoritative version and stored content so the workspace can keep the
    student's local text and offer a reconciliation rather than silently reloading over
    it. The content is the draft's own body, which the client already holds a version of,
    so returning it here leaks nothing new.
    """

    def __init__(self, current_version: int, current_content: str) -> None:
        super().__init__(
            "This draft changed somewhere else, so your latest edit was not saved yet.",
            extra={
                "code": "stale_body_version",
                "current_version": current_version,
                "server_body": current_content,
            },
        )
        self.current_version = current_version
        self.current_content = current_content


class UpstreamError(LyraError):
    """The user's tutor endpoint failed or misbehaved."""

    status = 502


class ToolsUnsupportedError(UpstreamError):
    """The endpoint will not accept tool definitions.

    Raised rather than returned because it can surface from anywhere in a request. It is
    a control signal more than a fault: the tool loop catches it and degrades, so solving
    still works against an endpoint that cannot verify. It reaches a response only if
    something asks for tools outside that loop.
    """


class ConfigurationError(LyraError):
    """Something the user must configure is missing or unusable."""

    status = 400

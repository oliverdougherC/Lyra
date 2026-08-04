"""The one shape every tool returns.

A tool error is a result, not an exception. A malformed expression, a timeout, or an
integral SymPy cannot do is information the model can act on: it can try a different
form, or report that the claim is not checkable. Raising instead would make the loop
decide, and the loop knows nothing about mathematics.

Only a bug in the tool machinery itself raises.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolResult:
    """What one tool call produced.

    Attributes:
        ok: Whether the computation ran. False carries `error` and an empty `value`.
        value: The result, JSON-serializable, shaped per tool. Empty when `ok` is False.
        error: A short reason, written for the model rather than for the user. It never
            carries a filesystem path, a traceback, or an endpoint.
    """

    ok: bool
    value: dict[str, object] = field(default_factory=dict)
    error: str = ""

    def as_payload(self) -> dict[str, object]:
        """The dict handed back to the model as the tool message's content."""
        return {"ok": self.ok, **self.value} if self.ok else {"ok": False, "error": self.error}


def failure(error: str) -> ToolResult:
    """A failed call carrying its reason."""
    return ToolResult(ok=False, error=error)


def success(**value: object) -> ToolResult:
    """A successful call carrying its named results."""
    return ToolResult(ok=True, value=value)

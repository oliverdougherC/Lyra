"""The tool-calling loop and the tool registry.

Written in-house against the existing client rather than adopted from an agent framework.
The tool surface is small, and what a framework adds beyond the loop itself is
multi-provider abstraction, plugin systems, sandboxing, and session state that Lyra
already has or explicitly excludes.

Built in Phase 2 because solution verification needs it, not in Phase 4 where the agent
lives. Four requirements, in the order they matter:

1. **Termination is guaranteed.** A call-depth ceiling and a wall-clock timeout. Hitting
   either is reported on the result, never smoothed over: a loop that silently stops
   producing is worse than one that says it gave up, because the caller would read the
   silence as agreement.
2. **Every call is recorded.** `ToolLoopResult.calls` is the transcript the interface
   renders. It is a debugging affordance now and the precondition for trusting the agent
   in Phase 4.
3. **Tools are pure.** The Phase 2 set only computes. A call to a name outside the
   registry is refused here and reported back to the model as a failed result.
4. **A tool error is a result, not an exception.** Bad arguments, an unknown tool, and a
   computation that could not be done all travel back to the model as something it can
   act on. Only a bug in this module raises.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from backend.core.errors import ToolsUnsupportedError
from backend.llm.client import AssistantMessage, ToolCall, complete_with_tools
from backend.tools import cas, units
from backend.tools.result import ToolResult, failure

logger = logging.getLogger(__name__)

# Rounds of tool calls, not individual calls: a model may ask for four checks at once and
# that is one round. Eight is well past what checking a single problem needs, and low
# enough that a model stuck in a loop stops costing time quickly.
MAX_DEPTH = 8

# Wall clock for one whole loop. Generous because a local model can spend minutes per
# turn, and this is a background job with nobody waiting on an open connection.
TIMEOUT_SECONDS = 600.0

COMPLETED = "completed"
DEPTH = "depth"
TIMEOUT = "timeout"
NO_TOOL_SUPPORT = "no_tool_support"
UPSTREAM_FAILED = "upstream_failed"

# Every reason a loop can end other than the model deciding it is finished. A caller must
# treat all of these as "the check did not run", never as agreement.
INCOMPLETE_REASONS: tuple[str, ...] = (DEPTH, TIMEOUT, NO_TOOL_SUPPORT, UPSTREAM_FAILED)

_UNKNOWN_TOOL = "There is no tool called {name}."
_BAD_ARGUMENTS = "The arguments were not valid JSON."
_MISSING_ARGUMENT = "Missing required argument: {name}."

_TIMEOUT_DETAIL = "Checking took too long and was stopped."
_DEPTH_DETAIL = "Checking stopped after {rounds} rounds of tool calls."
_UPSTREAM_DETAIL = "The tutor endpoint could not be reached."


@dataclass(frozen=True)
class ToolDefinition:
    """One tool the model may call.

    Attributes:
        name: The name the model calls, and the registry key.
        description: What it does, written for the model.
        parameters: JSON Schema for the arguments. Also the allowlist: an argument the
            schema does not declare is dropped before the handler is called, so a model
            inventing a keyword cannot turn into a `TypeError`.
        handler: The implementation. Synchronous and possibly blocking, so the loop runs
            it off the event loop.
    """

    name: str
    description: str
    parameters: dict[str, object]
    handler: Callable[..., ToolResult]

    @property
    def properties(self) -> dict[str, object]:
        """The declared argument names and their schemas."""
        declared = self.parameters.get("properties")
        return declared if isinstance(declared, dict) else {}

    @property
    def required(self) -> tuple[str, ...]:
        """Arguments the handler cannot run without."""
        declared = self.parameters.get("required")
        return tuple(str(name) for name in declared) if isinstance(declared, list) else ()

    def schema(self) -> dict[str, object]:
        """This tool in the shape the chat-completions API wants."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class RecordedCall:
    """One executed call, kept for the transcript.

    Attributes:
        name: Tool called, including a name that turned out not to exist.
        arguments: Parsed arguments, or an empty mapping when the model's JSON was
            unreadable.
        raw_arguments: What the model actually sent, kept so an unreadable call can still
            be shown rather than reduced to "invalid".
        ok: Whether the tool ran.
        result: The payload handed back to the model.
    """

    name: str
    arguments: dict[str, object]
    raw_arguments: str
    ok: bool
    result: dict[str, object]


@dataclass(frozen=True)
class ToolLoopResult:
    """What one loop produced, and why it stopped.

    Attributes:
        content: The model's final message.
        calls: Every tool call in the order it ran. The audit trail.
        stopped: `completed`, or one of `INCOMPLETE_REASONS`.
        detail: A sentence explaining an incomplete stop, empty otherwise.
    """

    content: str
    calls: tuple[RecordedCall, ...] = ()
    stopped: str = COMPLETED
    detail: str = ""

    @property
    def complete(self) -> bool:
        """Whether the model finished on its own terms."""
        return self.stopped == COMPLETED


def _tool(
    name: str, description: str, handler: Callable[..., ToolResult], **properties: object
) -> ToolDefinition:
    """Build a definition, with required arguments being those with no default marker."""
    required = [key for key, schema in properties.items() if _is_required(schema)]
    return ToolDefinition(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {key: _clean(schema) for key, schema in properties.items()},
            "required": required,
        },
        handler=handler,
    )


def _is_required(schema: object) -> bool:
    """An argument is required unless its schema is marked optional."""
    return not (isinstance(schema, dict) and schema.get("optional"))


def _clean(schema: object) -> object:
    """Drop Lyra's own `optional` marker, which is not part of JSON Schema."""
    if not isinstance(schema, dict):
        return schema
    return {key: value for key, value in schema.items() if key != "optional"}


_NUMBER_EXPRESSION = {
    "type": "string",
    "description": "A mathematical expression in plain notation, such as 3*x**2 + 1.",
}

REGISTRY: dict[str, ToolDefinition] = {
    definition.name: definition
    for definition in (
        _tool(
            "cas_evaluate",
            "Simplify an expression, or check whether two expressions are equal. Use this "
            "to check an identity or an algebraic step. When comparing, read `certain`: "
            "if it is false the two could not be settled either way, which is not a "
            "disagreement.",
            cas.evaluate,
            expression=_NUMBER_EXPRESSION,
            compare_to={
                "type": "string",
                "description": "A second expression to test the first against.",
                "optional": True,
            },
        ),
        _tool(
            "cas_solve",
            "Solve an equation or a system of equations for named unknowns.",
            cas.solve,
            equations={
                "type": "array",
                "items": {"type": "string"},
                "description": "Each as 'lhs = rhs', or a bare expression meaning it equals zero.",
            },
            unknowns={
                "type": "array",
                "items": {"type": "string"},
                "description": "Symbol names to solve for, such as ['x', 'y'].",
            },
        ),
        _tool(
            "cas_integrate",
            "Integrate an expression in one variable. Give both bounds for a definite "
            "integral, or neither for an indefinite one.",
            cas.integrate,
            expression=_NUMBER_EXPRESSION,
            variable={"type": "string", "description": "The variable of integration."},
            lower={"type": "string", "description": "Lower bound.", "optional": True},
            upper={"type": "string", "description": "Upper bound.", "optional": True},
        ),
        _tool(
            "cas_differentiate",
            "Differentiate an expression with respect to a variable, to any order.",
            cas.differentiate,
            expression=_NUMBER_EXPRESSION,
            variable={"type": "string", "description": "The variable to differentiate by."},
            order={
                "type": "integer",
                "description": "Order of the derivative, 1 to 10. Defaults to 1.",
                "optional": True,
            },
        ),
        _tool(
            "cas_linalg",
            "Linear algebra on a matrix: determinant, inverse, eigenvalues, rank, or "
            "solving a linear system.",
            cas.linalg,
            operation={
                "type": "string",
                "enum": list(cas.LINALG_OPERATIONS),
                "description": "Which computation to run.",
            },
            matrix={
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "description": "Rows of expression strings. Entries may be symbolic.",
            },
            vector={
                "type": "array",
                "items": {"type": "string"},
                "description": "Right-hand side, required only by 'solve'.",
                "optional": True,
            },
        ),
        _tool(
            "check_units",
            "Check that a quantity carries the expected dimensions. Compares dimensions "
            "rather than units, so metres and kilometres agree.",
            units.check,
            expression={
                "type": "string",
                "description": "A quantity with units, such as '9.8 m/s^2'.",
            },
            expected={
                "type": "string",
                "description": "The units the value should carry, such as 'm/s^2' or 'N'.",
            },
        ),
    )
}


def tool_schemas() -> list[dict[str, object]]:
    """Every registered tool, in the shape the chat-completions API wants."""
    return [definition.schema() for definition in REGISTRY.values()]


def _parse_arguments(raw: str) -> dict[str, object] | None:
    """Read the model's argument JSON, or None when it is not a usable object."""
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _dispatch(call: ToolCall) -> RecordedCall:
    """Run one tool call, turning every failure into a result the model can act on."""
    definition = REGISTRY.get(call.name)
    if definition is None:
        # The registry is the whole allowlist. Nothing outside it is reachable, whatever
        # the model asks for.
        return _recorded(call, {}, failure(_UNKNOWN_TOOL.format(name=call.name)))

    arguments = _parse_arguments(call.arguments)
    if arguments is None:
        return _recorded(call, {}, failure(_BAD_ARGUMENTS))

    # The schema is the allowlist for keyword names too, so an invented argument is
    # dropped rather than becoming a TypeError the model cannot see or fix.
    accepted = {key: value for key, value in arguments.items() if key in definition.properties}
    missing = [name for name in definition.required if name not in accepted]
    if missing:
        return _recorded(call, accepted, failure(_MISSING_ARGUMENT.format(name=missing[0])))

    try:
        result = definition.handler(**accepted)
    except Exception:
        # A handler is supposed to return a failed result rather than raise. One that
        # raises anyway is a bug here, and it must still not take the loop down.
        logger.exception("Tool %s raised instead of returning a result", call.name)
        return _recorded(call, accepted, failure("That check could not be run."))
    return _recorded(call, accepted, result)


def _recorded(call: ToolCall, arguments: dict[str, object], result: ToolResult) -> RecordedCall:
    """Pair one call with its outcome for the transcript."""
    return RecordedCall(
        name=call.name,
        arguments=arguments,
        raw_arguments=call.arguments,
        ok=result.ok,
        result=result.as_payload(),
    )


def _assistant_turn(answer: AssistantMessage) -> dict[str, object]:
    """The model's own turn, in the shape it has to be replayed as history."""
    return {
        "role": "assistant",
        "content": answer.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in answer.tool_calls
        ],
    }


def _tool_turn(call: ToolCall, recorded: RecordedCall) -> dict[str, object]:
    """One tool's answer, addressed to the call that asked for it."""
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": json.dumps(recorded.result, separators=(",", ":")),
    }


async def run_tool_loop(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict[str, object]],
    *,
    max_depth: int = MAX_DEPTH,
    timeout_seconds: float = TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolLoopResult:
    """Run the model with tools until it stops asking for them, or until a ceiling.

    Args:
        endpoint: Endpoint base URL including its version suffix.
        api_key: Bearer token, or None when the endpoint needs no auth.
        model: Model identifier, omitted from the request when None.
        messages: The conversation to start from. Copied, not mutated.
        max_depth: Rounds of tool calls allowed. One round may hold several calls.
        timeout_seconds: Wall-clock ceiling for the whole loop.
        transport: Test seam. Leave unset in production code.

    Returns:
        The model's final content, every call that ran, and why the loop stopped. A
        `stopped` value in `INCOMPLETE_REASONS` means the model did not finish, and the
        caller must not read the content as a conclusion. Calls made before the stop are
        still returned: partial work is worth showing, it is just not worth trusting as
        an answer.

    Raises:
        Nothing. Upstream failures are reported through `stopped`, because a verification
        pass that cannot reach the model is a check that did not run, not an error the
        student needs to see.
    """
    # The timeout wraps the awaits rather than being checked between rounds, so it holds
    # while a single model call is hanging. Checking between rounds would let one stalled
    # call sit for the client's full read timeout with the ceiling already passed.
    calls: list[RecordedCall] = []
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _drive(
                endpoint, api_key, model, list(messages), calls, max_depth, transport
            )
    except TimeoutError:
        # `calls` is built outside the block precisely so a cut-off loop still reports
        # the checks that did run.
        return ToolLoopResult(
            content="", calls=tuple(calls), stopped=TIMEOUT, detail=_TIMEOUT_DETAIL
        )


async def _drive(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    conversation: list[dict[str, object]],
    calls: list[RecordedCall],
    max_depth: int,
    transport: httpx.AsyncBaseTransport | None,
) -> ToolLoopResult:
    """The loop itself. Appends to `calls` as it goes, so a caller can read them on a cut."""
    tools = tool_schemas()
    for _ in range(max_depth):
        try:
            answer = await complete_with_tools(
                endpoint, api_key, model, conversation, tools, transport=transport
            )
        except ToolsUnsupportedError as exc:
            return ToolLoopResult(
                content="", calls=tuple(calls), stopped=NO_TOOL_SUPPORT, detail=exc.message
            )
        except Exception as exc:
            # CancelledError is a BaseException, so the timeout above still passes through.
            logger.warning("Tool loop could not reach the tutor endpoint: %s", type(exc).__name__)
            return ToolLoopResult(
                content="",
                calls=tuple(calls),
                stopped=UPSTREAM_FAILED,
                detail=getattr(exc, "message", _UPSTREAM_DETAIL),
            )

        if not answer.tool_calls:
            # The model is finished. This is the only path that returns `completed`.
            return ToolLoopResult(content=answer.content, calls=tuple(calls), stopped=COMPLETED)

        conversation.append(_assistant_turn(answer))
        for call in answer.tool_calls:
            # Handlers block on a subprocess, so they run off the event loop.
            recorded = await asyncio.to_thread(_dispatch, call)
            calls.append(recorded)
            conversation.append(_tool_turn(call, recorded))

    return ToolLoopResult(
        content="",
        calls=tuple(calls),
        stopped=DEPTH,
        detail=_DEPTH_DETAIL.format(rounds=max_depth),
    )

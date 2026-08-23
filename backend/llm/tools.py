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
3. **The registry is the allowlist.** A call to a name outside it is refused here and
   reported back to the model as a failed result. The default registry is the Phase 2
   set, which only computes; a caller may pass its own (the writer's tools read and
   propose through the database), and purity is then a property of that registry, not
   a promise of the loop. What the loop does promise is the transcript: every call,
   whichever registry it came from, lands in `ToolLoopResult.calls`.
4. **A tool error is a result, not an exception.** Bad arguments, an unknown tool, and a
   computation that could not be done all travel back to the model as something it can
   act on. Only a bug in this module raises.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from backend.core.errors import ToolsUnsupportedError
from backend.llm import replies
from backend.llm.client import (
    DETERMINISTIC_TEMPERATURE,
    AssistantMessage,
    ToolCall,
    complete_with_tools,
)
from backend.rag.tokens import estimate_tokens
from backend.tools import cas, units
from backend.tools.result import ToolResult, failure

logger = logging.getLogger(__name__)

# Rounds of tool calls, not individual calls: a model may ask for four checks at once and
# that is one round.
#
# Twenty-four rather than a tighter number, because a homework problem is routinely five
# lettered sub-parts and a checker works through them one at a time. Measured against a
# real ECE signals set, checking one such problem took fifteen rounds; a ceiling of eight
# cut it off mid-way and the verdict came back `unchecked` every time, which is honest but
# useless. The wall clock below is what actually bounds the loop; this is the backstop for
# a model that has stopped making progress.
MAX_DEPTH = 24

# Wall clock for one whole loop. Generous because a local model can spend minutes per
# turn, and this is a background job with nobody waiting on an open connection. It is the
# ceiling that bounds a run: `client.TOOL_TIMEOUT` matches it rather than cutting in
# front of it, so a slow turn late in a long transcript is cut here, reported as a
# timeout with the checks it did run, rather than surfacing as an endpoint failure.
TIMEOUT_SECONDS = 600.0

COMPLETED = "completed"
DEPTH = "depth"
TIMEOUT = "timeout"
NO_TOOL_SUPPORT = "no_tool_support"
UPSTREAM_FAILED = "upstream_failed"
CONTEXT_OVERFLOW = "context_overflow"

# Every reason a loop can end other than the model deciding it is finished. A caller must
# treat all of these as "the check did not run", never as agreement.
INCOMPLETE_REASONS: tuple[str, ...] = (
    DEPTH,
    TIMEOUT,
    NO_TOOL_SUPPORT,
    UPSTREAM_FAILED,
    CONTEXT_OVERFLOW,
)

_UNKNOWN_TOOL = "There is no tool called {name}."
_BAD_ARGUMENTS = "The arguments were not valid JSON."
_MISSING_ARGUMENT = "Missing required argument: {name}."

_TIMEOUT_DETAIL = "Checking took too long and was stopped."
_DEPTH_DETAIL = "Checking stopped after {rounds} rounds of tool calls."
_UPSTREAM_DETAIL = "The tutor endpoint could not be reached."
_MID_LOOP_DETAIL = (
    "The tutor endpoint rejected a request partway through checking, most likely because "
    "the tool transcript outgrew the model's context window."
)
# Said when the local guard stops the loop before a request the transcript has grown too
# large for. Bounded and generic: it names no endpoint, tool argument, or private text.
_OVERFLOW_DETAIL = (
    "The tool transcript filled the model's context window before the turn could finish. "
    "Try a shorter request or a narrower scope."
)


@dataclass(frozen=True)
class ContextBudget:
    """The window a growing tool loop must keep its next request inside.

    An agent turn's first request is proved to fit before it is sent (the route's
    preflight), but each round appends the model's tool calls and their results, so a
    later request can outgrow the window even though the first one fit. This carries the
    numbers the loop re-checks against before every subsequent request: the configured
    window, the generation reserve held back for the reply, and the constant
    tool-definition overhead sent on every request (measured once by the caller, from the
    same registry the loop runs, so the guard and the preflight agree on it).

    A loop given no budget does not guard - the writer chat and the solver's verification
    pass bound their transcripts by depth and wall clock instead - so this is opt-in and
    changes nothing for callers that omit it.
    """

    context_window: int
    generation_reserve: int
    tool_tokens: int

    @property
    def message_ceiling(self) -> int:
        """The most the conversation itself may cost, once the reply and tools are set aside."""
        return self.context_window - self.generation_reserve - self.tool_tokens


def schema_tokens(tools: list[dict[str, object]]) -> int:
    """Estimate the tokens a tool-definition list adds to every request.

    Measured on the same JSON shape `complete_with_tools` sends and with the one shared
    estimator, so the tool overhead a route charges in preflight is the overhead the loop
    guards against round after round.
    """
    return estimate_tokens(json.dumps(tools, separators=(",", ":")))


def _message_tokens(message: Mapping[str, object]) -> int:
    """Estimate one conversation message's cost, content and any tool-call payload alike.

    An assistant turn that asked for tools carries the calls it made, and a tool turn
    carries the result handed back; both re-enter the model context on the next request,
    so both are charged here rather than only the visible `content`.
    """
    total = 0
    content = message.get("content")
    if content:
        total += estimate_tokens(str(content))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += estimate_tokens(json.dumps(tool_calls, separators=(",", ":")))
    name = message.get("name")
    if name:
        total += estimate_tokens(str(name))
    return total


def _conversation_tokens(conversation: list[dict[str, object]]) -> int:
    """The whole conversation's estimated cost, summed message by message."""
    return sum(_message_tokens(message) for message in conversation)


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


# Naming the notation is cheaper than refusing it. Measured against a real signals set, the
# checker reached for `2x`, `i` for the imaginary unit, and `u(t)` for the unit step; two of
# those now work and the third has to be written differently, so the description says which.
_NUMBER_EXPRESSION = {
    "type": "string",
    "description": (
        "A mathematical expression in plain notation, such as 3*x**2 + 1. Write every "
        "multiplication: `2*x`, not `2x`. `u(t)` is the unit step and `delta(t)` the "
        "impulse. Use `I` or `j` for the imaginary unit, never `i`. `oo` or `inf` is "
        "infinity. `integrate(f, t, a, b)`, `diff(f, t)` and `limit(f, t, a)` may be used "
        "inside an expression, which is how to compare an integral against a closed form."
    ),
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


def tool_schemas(registry: dict[str, ToolDefinition] | None = None) -> list[dict[str, object]]:
    """Every registered tool, in the shape the chat-completions API wants."""
    definitions = REGISTRY if registry is None else registry
    return [definition.schema() for definition in definitions.values()]


def _parse_arguments(raw: str) -> dict[str, object] | None:
    """Read the model's argument JSON, or None when it is not a usable object.

    Through `replies`, so an expression a model wrote in the notation it had been reading —
    `\\frac{1}{2}` rather than `1/2` — reaches the tool and is refused there, with a message
    saying what was wrong with it, rather than being dropped as unparseable arguments.
    """
    parsed = replies.loads(raw or "{}")
    return parsed if isinstance(parsed, dict) else None


def _dispatch(call: ToolCall, registry: dict[str, ToolDefinition]) -> RecordedCall:
    """Run one tool call, turning every failure into a result the model can act on."""
    definition = registry.get(call.name)
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

    recorded = _recorded(call, accepted, result)
    try:
        # Proved here, where a failure is still one tool's failure. The loop serializes
        # this payload twice afterwards, into the model's turn and into the audit trail,
        # and neither of those is inside a guard: an unserializable result raised there
        # instead travelled out of the loop and cost the whole verification pass, taking
        # every check that had already run with it.
        json.dumps(recorded.result)
    except (TypeError, ValueError):
        logger.warning("Tool %s returned a result that cannot be serialized", call.name)
        return _recorded(call, accepted, failure("That check produced a result Lyra cannot read."))
    return recorded


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
    registry: dict[str, ToolDefinition] | None = None,
    on_call: Callable[[RecordedCall], None] | None = None,
    context_budget: ContextBudget | None = None,
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
        registry: The tools on offer. Defaults to the Phase 2 computation set; the
            writer passes its own. Whatever is passed is the whole allowlist.
        on_call: Invoked with each recorded call as it lands, so a streaming caller can
            narrate the loop while it runs. Runs on the event loop thread: keep it
            cheap, and it must not raise - one that does is logged and ignored, because
            a narration bug must not cost the pass.
        context_budget: When given, the loop re-measures the conversation before every
            request after the first and stops with `CONTEXT_OVERFLOW` rather than sending
            one the accumulated transcript has grown too large for. The first request is
            not re-checked here: its fit is the caller's preflight to prove. Omit it to
            leave depth and wall clock as the only bounds, as the writer and solver do.
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
                endpoint,
                api_key,
                model,
                list(messages),
                calls,
                max_depth,
                REGISTRY if registry is None else registry,
                on_call,
                context_budget,
                transport,
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
    registry: dict[str, ToolDefinition],
    on_call: Callable[[RecordedCall], None] | None,
    context_budget: ContextBudget | None,
    transport: httpx.AsyncBaseTransport | None,
) -> ToolLoopResult:
    """The loop itself. Appends to `calls` as it goes, so a caller can read them on a cut."""
    tools = tool_schemas(registry)
    for round_index in range(max_depth):
        # After the first request, the appended assistant turns and tool results can have
        # grown the conversation past the window even though round zero fit. Re-measure the
        # exact next request and stop before sending one that cannot fit, rather than
        # letting the endpoint reject it (the `ToolsUnsupportedError` branch below) or,
        # worse, silently truncate it. Round zero is skipped: proving it fits is the
        # caller's preflight, and re-checking it here could only disagree with that.
        if (
            context_budget is not None
            and round_index
            and _conversation_tokens(conversation) > context_budget.message_ceiling
        ):
            return ToolLoopResult(
                content="",
                calls=tuple(calls),
                stopped=CONTEXT_OVERFLOW,
                detail=_OVERFLOW_DETAIL,
            )
        try:
            answer = await complete_with_tools(
                endpoint,
                api_key,
                model,
                conversation,
                tools,
                transport=transport,
                temperature=DETERMINISTIC_TEMPERATURE,
            )
        except ToolsUnsupportedError as exc:
            if round_index:
                # The client reads every 400 on a tools-carrying request as "no tool
                # support", and on the first request that is the best available reading.
                # It cannot be the right one here: this endpoint has already answered
                # tool rounds in this very loop, so a 400 now is the request going bad -
                # a transcript grown past the context window is the common way - and
                # calling it "does not accept tool calls" would have the settings screen
                # and the verdict contradict each other over a capability that was just
                # demonstrated.
                return ToolLoopResult(
                    content="",
                    calls=tuple(calls),
                    stopped=UPSTREAM_FAILED,
                    detail=_MID_LOOP_DETAIL,
                )
            return ToolLoopResult(
                content="", calls=tuple(calls), stopped=NO_TOOL_SUPPORT, detail=exc.message
            )
        except Exception as exc:
            # CancelledError is a BaseException, so the timeout above still passes through.
            # `exception` rather than `warning`: this branch catches real bugs alongside
            # network weather, and a class name with no traceback made the two
            # indistinguishable in the log.
            logger.exception("Tool loop could not reach the tutor endpoint")
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
            # Handlers block on a subprocess, so they run off the event loop. Known cost:
            # `to_thread` cannot be cancelled once the handler is running, so when the
            # wall clock above cuts the loop mid-dispatch, a hung sympy call keeps its
            # worker thread until it finishes on its own. Tolerated because the handlers
            # bound themselves (cas runs under its own subprocess timeout), verification
            # is rare enough that a leak per timeout does not accumulate, and the
            # alternative - a kill-able subprocess per call - buys a daemon thread's
            # worth of safety at a process-management price this loop does not yet earn.
            recorded = await asyncio.to_thread(_dispatch, call, registry)
            calls.append(recorded)
            conversation.append(_tool_turn(call, recorded))
            if on_call is not None:
                try:
                    on_call(recorded)
                except Exception:
                    # Narration is an observer, never a participant: a broken callback
                    # is logged and the pass continues as if it were absent.
                    logger.exception("on_call callback raised; the loop continues")

    return ToolLoopResult(
        content="",
        calls=tuple(calls),
        stopped=DEPTH,
        detail=_DEPTH_DETAIL.format(rounds=max_depth),
    )

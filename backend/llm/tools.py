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
import contextlib
import json
import logging
import threading
import time
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
from backend.llm.turn_budget import input_ceiling
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
OUTPUT_LIMIT = "output_limit"
# The loop was stopped on purpose (the UI's Stop) rather than by a ceiling: the caller
# settles the turn as stopped, never as a failure the student should retry on their own.
STOPPED = "stopped"

# Bounded how long a cancelled loop waits for an in-flight tool worker to leave before it
# reports settlement. Every agent handler bounds its own work (network calls carry their
# own timeouts, proposals are single writes), so this is a backstop, not a working number:
# if it expires, the turn settles anyway and the stop flag has already made any durable
# effect the worker might land impossible.
QUIESCENCE_SECONDS = 90.0

# Every reason a loop can end other than the model deciding it is finished. A caller must
# treat all of these as "the check did not run", never as agreement.
INCOMPLETE_REASONS: tuple[str, ...] = (
    DEPTH,
    TIMEOUT,
    NO_TOOL_SUPPORT,
    UPSTREAM_FAILED,
    CONTEXT_OVERFLOW,
    OUTPUT_LIMIT,
    STOPPED,
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
# Said when the endpoint cut a guarded round off at the reserved output ceiling. The loop
# caps each guarded request at the exact generation reserve it budgeted, so a
# `finish_reason` of "length" means the reply is not finished: partial prose is not an
# answer, and a half-written tool call must never be dispatched. Bounded and generic: it
# names no endpoint, tool argument, or private text.
_OUTPUT_LIMIT_DETAIL = (
    "The reply reached the space reserved for it before the turn could finish. Try a "
    "shorter request or a narrower scope."
)
_STOPPED_DETAIL = "This turn was stopped."


class ToolStopGate:
    """One turn's stop/quiescence state, shared between the event loop and its workers.

    A tool handler runs in a worker thread (`asyncio.to_thread`), and Python cannot cancel
    a thread that is already executing - so "the task was cancelled" is not, by itself,
    the truth that "the tool work has stopped". This gate is the shared half of the Stop
    contract, and it makes that truth hold for a turn's in-flight workers:

    * `request_stop` latches a flag the handlers read at their durable boundaries. Once it
      is set, no already-running tool can *create* a new durable consequence (a source, a
      change proposal, a command, an access request): each such tool re-checks the flag
      before its write and refuses when it is set. The flag is the guarantee; the waiting
      below is only about knowing when the worker has actually left.
    * Workers register their lifetime with `begin_work`/`finish_work` (the loop's planning
      pass and every tool dispatch), and `wait_quiesced` blocks until none are inside a
      dispatch. Stop therefore reports completion only once every worker has left, and a
      request that is being torn down cannot close its database connection out from under
      a worker that is still reading or writing.

    The gate is plain threading state because it is crossed between the event-loop thread
    and worker threads; it is per-turn and dies with the turn's in-flight entry.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: list[threading.Event] = []

    def request_stop(self) -> None:
        """Latch the stop flag. Durable-effect checks in every handler read it from here."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def begin_work(self) -> threading.Event:
        """Register one worker as in flight; return its completion event.

        Called by the worker (or the loop before `to_thread`), on any thread.
        """
        done = threading.Event()
        with self._lock:
            self._in_flight.append(done)
        return done

    def finish_work(self, done: threading.Event) -> None:
        """Clear the registration and signal the worker has left, on any thread."""
        with contextlib.suppress(ValueError), self._lock:
            self._in_flight.remove(done)
        done.set()

    @property
    def in_flight(self) -> bool:
        with self._lock:
            return bool(self._in_flight)

    def wait_quiesced(self, timeout: float | None) -> bool:
        """Block until no worker is inside a dispatch.

        True when quiesced before `timeout` expires; with `timeout=None` there is no
        deadline and the call returns only once every registered worker has left - the
        form the turn's release wait detaches to when its request is torn down first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                pending = [event for event in self._in_flight if not event.is_set()]
            if not pending:
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
            else:
                remaining = None
            # Wait on one; a second worker may have started in the meantime, so re-check.
            if pending[0].wait(remaining):
                continue
            return False


@dataclass(frozen=True)
class ContextBudget:
    """The window a growing tool loop must keep its next request inside.

    An agent turn's first request is proved to fit before it is sent (the route's
    preflight), but each round appends the model's tool calls and their results, so a
    later request can outgrow the window even though the first one fit. This carries the
    numbers the loop re-checks against as the transcript grows: the configured window, the
    generation reserve held back for the reply, the constant tool-definition overhead sent
    on every request (measured once by the caller, from the same registry the loop runs, so
    the guard and the preflight agree on it), and the estimator safety margin the preflight
    accepted the first request under.

    `safety_margin` is carried so the loop guards later requests exactly as conservatively
    as the preflight admitted the first: both fold it into `input_ceiling`, so a request
    the preflight would have accepted is not one the loop then rejects for measuring the
    window differently. It defaults to zero, which reproduces the pre-margin ceiling
    unchanged, so callers that never set it (the solver's verification pass, and any test
    that constructs a bare budget) are unaffected.

    A loop given no budget does not guard - the writer chat and the solver's verification
    pass bound their transcripts by depth and wall clock instead - so this is opt-in and
    changes nothing for callers that omit it.
    """

    context_window: int
    generation_reserve: int
    tool_tokens: int
    safety_margin: float = 0.0

    @property
    def message_ceiling(self) -> int:
        """The most the conversation itself may cost, once the reply and tools are set aside.

        Folds in the safety margin through the same `input_ceiling` the preflight uses, so
        the loop and the preflight bound the request the same way. With `safety_margin`
        left at zero this is `context_window - generation_reserve - tool_tokens`, the ceiling
        the guard used before the margin existed.
        """
        return (
            input_ceiling(self.context_window, self.generation_reserve, margin=self.safety_margin)
            - self.tool_tokens
        )


def schema_tokens(tools: list[dict[str, object]]) -> int:
    """Estimate the tokens a tool-definition list adds to every request.

    Measured on the same JSON shape `complete_with_tools` sends and with the one shared
    estimator, so the tool overhead a route charges in preflight is the overhead the loop
    guards against round after round.
    """
    return estimate_tokens(json.dumps(tools, separators=(",", ":")))


def message_tokens(message: Mapping[str, object]) -> int:
    """Estimate one conversation message's cost from the exact shape sent to the endpoint.

    `client._chat_body` places each message dict into the request `messages` array
    verbatim, so the tokens it costs are the tokens of its compact JSON serialization -
    every field the request carries (`role`, `content`, a `tool_calls` payload, a tool
    turn's `tool_call_id` and `name`) and the object/array/key/string framing around them,
    not a hand-picked subset. Measuring the whole serialized shape is what lets the
    preflight and the loop claim to charge the same request the client will send: an
    earlier version summed only `content`, `tool_calls`, and `name`, so across many short
    messages and long tool-call ids the omitted framing accumulated into a real
    undercount. The tool-definition list is charged once, separately (`schema_tokens` and
    `ContextBudget.tool_tokens`); it is not part of any message, so nothing here
    double-counts it.
    """
    return estimate_tokens(json.dumps(message, separators=(",", ":"), default=str))


def conversation_tokens(conversation: list[dict[str, object]]) -> int:
    """The whole `messages` array's estimated cost, measured in one serialization.

    The one accounting path the agent preflight and the loop's growth guard both call, so
    the request one measures is the request the other measures. It measures the compact
    JSON of the entire array `client._chat_body` places under `messages` - the object
    framing of every message *and* the `[` `]` and inter-message commas that join them - so
    it is literally the wire shape of that field, not a per-message sum that quietly drops
    the separators. (`message_tokens` remains for charging one message in isolation, e.g.
    the fixed system and current-message costs the assembler sets aside first; it and this
    helper agree on message framing and differ only by the array's own framing, which this
    helper is the one that must include.) The tool schema sent alongside `messages` is
    charged once, separately, by the caller.
    """
    return estimate_tokens(json.dumps(conversation, separators=(",", ":"), default=str))


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
    stop_gate: ToolStopGate | None = None,
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
        context_budget: When given, the loop does two things an unguarded loop does not.
            It caps every request at the budgeted `generation_reserve` output tokens, so the
            reserve held back in the context arithmetic is the reserve the endpoint is
            actually told to stay inside; a round the endpoint cuts off at that ceiling
            (`finish_reason: "length"`) stops the loop with `OUTPUT_LIMIT` rather than being
            read as a finished answer or dispatched as a half-written tool call. And it
            re-measures the conversation at every point it grows - after the model's tool-call
            turn is appended, after each individual tool result is appended, and before every
            request after the first - and stops with `CONTEXT_OVERFLOW` the moment the
            transcript can no longer continue, rather than dispatching further tools or
            sending a request the accumulated transcript has outgrown. The first request
            itself is not re-checked for fit: that is the caller's preflight to prove. Omit
            the budget to leave depth and wall clock as the only bounds, and to send no output
            ceiling, as the writer and solver do.
        stop_gate: One turn's stop/quiescence state. When given, the loop settles with
            `stopped` the moment the turn's Stop latches, waits for an in-flight tool
            dispatch to quiesce before a cancellation propagates, and runs every dispatch
            under the gate's in-flight accounting so the caller can wait for quiescence
            before reporting the turn settled. Omit to leave the loop exactly as it ran
            before the gate existed, as the writer and solver do.
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
                stop_gate,
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
    stop_gate: ToolStopGate | None,
    transport: httpx.AsyncBaseTransport | None,
) -> ToolLoopResult:
    """The loop itself. Appends to `calls` as it goes, so a caller can read them on a cut."""
    tools = tool_schemas(registry)

    # A guarded loop reserves output room in its context arithmetic; that reservation is
    # only real if the endpoint is actually told to cap the reply at it, so the same reserve
    # becomes the request's `max_tokens`. Without a budget the reserve does not exist and the
    # ceiling is left off, so the writer chat and the solver's verification pass keep sending
    # exactly what they sent before.
    max_tokens = context_budget.generation_reserve if context_budget is not None else None

    def overflowed() -> bool:
        """Whether the conversation as it stands can no longer fit the next request.

        Re-read after every growth boundary, not only between rounds: a single assistant
        response can append its tool-call turn and several tool results, and any one of
        those can be the append that makes continuation impossible.
        """
        return (
            context_budget is not None
            and conversation_tokens(conversation) > context_budget.message_ceiling
        )

    for round_index in range(max_depth):
        # The turn's Stop may have latched since the last check (the stop endpoint sets the
        # flag before it cancels the task). When it has, no further model call or dispatch
        # belongs to this turn: settle as stopped, with the partial transcript kept for the
        # same reason a cut loop keeps its partial calls.
        if stop_gate is not None and stop_gate.stopped:
            return ToolLoopResult(
                content="",
                calls=tuple(calls),
                stopped=STOPPED,
                detail=_STOPPED_DETAIL,
            )
        # Before every request after the first, the appended assistant turns and tool
        # results can have grown the conversation past the window even though round zero
        # fit. Stop before sending one that cannot fit, rather than letting the endpoint
        # reject it (the `ToolsUnsupportedError` branch below) or, worse, silently truncate
        # it. Round zero is skipped: proving it fits is the caller's preflight, and
        # re-checking it here could only disagree with that. The intra-round checks below
        # already catch growth as it happens, so by the time control reaches here the
        # transcript is normally known to fit; this stays as the explicit guard at the
        # request boundary so no future edit can send an unmeasured request.
        if round_index and overflowed():
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
                max_tokens=max_tokens,
            )
        except ToolsUnsupportedError as exc:
            if round_index:
                # The client reads a non-context 400 on a tools-carrying request as "no tool
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
            # indistinguishable in the log. It also catches the case PLA-290 must classify
            # truthfully: a first-request context-window rejection. The client raises that as a
            # bare `UpstreamError` (never `ToolsUnsupportedError`), so it lands here and settles
            # as `UPSTREAM_FAILED`, not `NO_TOOL_SUPPORT` - the endpoint plainly processed the
            # tools field, it just could not fit the prompt. `exc.message` is a Lyra-written
            # constant; the endpoint's own prose was classified and dropped inside the client,
            # so nothing private travels out of here, not even in the logged traceback.
            logger.exception("Tool loop could not complete a tutor endpoint request")
            return ToolLoopResult(
                content="",
                calls=tuple(calls),
                stopped=UPSTREAM_FAILED,
                detail=getattr(exc, "message", _UPSTREAM_DETAIL),
            )

        if context_budget is not None and answer.truncated:
            # The endpoint cut this round off at the reserved output ceiling the loop sent as
            # `max_tokens`. A truncated reply is not a finished turn: its prose is a fragment,
            # not an answer to store, and its tool calls may be half-written, so none may be
            # dispatched. Settle honestly rather than treating the fragment as `COMPLETED` or
            # speculating on an incomplete tool request. Only guarded loops impose the ceiling,
            # so only they read truncation this way; an unguarded loop that never set a ceiling
            # keeps its prior behaviour.
            return ToolLoopResult(
                content="",
                calls=tuple(calls),
                stopped=OUTPUT_LIMIT,
                detail=_OUTPUT_LIMIT_DETAIL,
            )

        if not answer.tool_calls:
            # The model is finished. This is the only path that returns `completed`.
            return ToolLoopResult(content=answer.content, calls=tuple(calls), stopped=COMPLETED)

        conversation.append(_assistant_turn(answer))
        # The assistant's tool-call payload has now re-entered the transcript, and it can be
        # large enough on its own to make any continuation impossible. Check before
        # dispatching a single tool: if the turn cannot continue, none of the tools it asked
        # for should run, because their results would only be discarded with the transcript.
        if overflowed():
            return ToolLoopResult(
                content="",
                calls=tuple(calls),
                stopped=CONTEXT_OVERFLOW,
                detail=_OVERFLOW_DETAIL,
            )
        for call in answer.tool_calls:
            # Handlers block on a subprocess or the network, so they run off the event
            # loop. Known cost: `to_thread` cannot be cancelled once the handler is
            # running. For the loop's wall-clock cut that stays tolerated as before (the
            # handlers bound themselves). For a *Stop* it is not tolerated in that form:
            # the turn's stop flag is latched before the task is cancelled, and every
            # durable-effect tool re-checks it before its write, so a worker that
            # outlives the Stop can at most finish in-memory work - it cannot land a new
            # source, proposal, command, or access request. The in-flight registration
            # below is what lets the caller tell "the Stop is over" from "the worker has
            # actually left".
            # `call` is bound as a default argument so the worker's closure cannot observe
            # the loop variable the next iteration rebinds (the worker may still be inside
            # `_dispatch` when the loop advances past this iteration's await).
            def run_dispatch(call=call) -> RecordedCall:
                done = stop_gate.begin_work() if stop_gate is not None else None
                try:
                    return _dispatch(call, registry)
                finally:
                    if done is not None:
                        stop_gate.finish_work(done)

            try:
                recorded = await asyncio.to_thread(run_dispatch)
            except asyncio.CancelledError:
                # A cancellation landing mid-dispatch cannot stop the worker (threads are
                # not cancellable); it can only learn when the worker has left. The stop
                # flag is already latched, so the durable-effect contract holds either way
                # - this wait is what makes the turn's settlement truthful. Shielded: a
                # second cancellation (the request tearing down) may interrupt it, and
                # then the route's own gate wait is the bounded backstop.
                if stop_gate is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(
                            asyncio.to_thread(stop_gate.wait_quiesced, QUIESCENCE_SECONDS)
                        )
                raise
            calls.append(recorded)
            conversation.append(_tool_turn(call, recorded))
            # The flag may have latched while this worker ran: the calls still queued in
            # this same assistant reply belong to a turn that no longer has one.
            if stop_gate is not None and stop_gate.stopped:
                return ToolLoopResult(
                    content="",
                    calls=tuple(calls),
                    stopped=STOPPED,
                    detail=_STOPPED_DETAIL,
                )
            if on_call is not None:
                try:
                    on_call(recorded)
                except Exception:
                    # Narration is an observer, never a participant: a broken callback
                    # is logged and the pass continues as if it were absent.
                    logger.exception("on_call callback raised; the loop continues")
            # This result may be the one that fills the window. Stop the moment it does,
            # before dispatching the next call in this same assistant response: those calls
            # would run only to have their results discarded with a transcript that can no
            # longer be sent. The work that genuinely ran stays in `calls`; the loop simply
            # settles here rather than replaying a half-finished tool set as a request with
            # missing results.
            if overflowed():
                return ToolLoopResult(
                    content="",
                    calls=tuple(calls),
                    stopped=CONTEXT_OVERFLOW,
                    detail=_OVERFLOW_DETAIL,
                )

    return ToolLoopResult(
        content="",
        calls=tuple(calls),
        stopped=DEPTH,
        detail=_DEPTH_DETAIL.format(rounds=max_depth),
    )

"""One shared turn-budget contract for tutor chat, writer chat, and agent chat.

The three routes assemble different prompt material - the tutor grounds a reply in
retrieved course material, the writer drives a tool loop over a draft, the class agent
drives a tool loop with research/workspace/command tools - but they answer the same
question before sending anything: does one turn fit the endpoint's configured context
window beside the room the model needs to reply?

The pieces that answer it live here so the routes cannot drift into disagreeing about
what fits: the four-bucket split (`plan_budget`), the newest-first history trim
(`trim_history`) and the pair it may never drop (`MINIMUM_HISTORY_MESSAGES`), and the
fit inequality itself (`TurnReserve`). `backend/api/routes_chat.py` (tutor),
`backend/api/routes_drafts.py` (writer), and `backend/api/routes_agent_chat.py` (agent)
all import from here rather than re-deriving a weaker approximation.

This module lives under `llm/` rather than `api/` for two reasons: `core/` and `llm/`
may not import `api/`, and the agent's per-round context guard in `llm/tools.py` reuses
the same estimator and reserve this module budgets against.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from backend.llm.budget import GENERATION_SHARE
from backend.rag.tokens import estimate_tokens

# The four buckets of the context budget table in docs/rag-pipeline.md. They sum to 1.0,
# and an 8192 window divides into 2048 generation, 1229 system, 1638 history, and 3277
# retrieval. `GENERATION_SHARE` is imported rather than redeclared so the reserve the
# chat turn takes off the top is the same number the background pipelines reserve.
SYSTEM_SHARE = 0.15
HISTORY_SHARE = 0.20
RETRIEVAL_SHARE = 0.40

# The latest exchange survives any budget. A reply to a question whose question is gone
# reads as a non sequitur, which is worse than overrunning an estimate by one exchange.
MINIMUM_HISTORY_MESSAGES = 2

# Estimator/framing safety margin for the agent's context guard.
#
# `estimate_tokens` counts four characters per token against a tutor endpoint whose real
# tokenizer is unknown at build time (docs/rag-pipeline.md, Stage 7). Four characters per
# token is close for ordinary English prose; dense JSON, source code, equations, and
# non-ASCII text tokenize more densely, so the estimate can undershoot the true count. The
# canonical wire-shape accounting in `llm/tools.py` already measures message framing
# exactly (in characters), so what remains uncharged is only the characters-per-token
# ratio being worse than four - not the shape of the request.
#
# This margin is charged on the agent's *input* estimate (messages plus tool schema) so a
# turn the preflight accepts stays inside the window even when the endpoint's tokenizer
# runs a little denser than the estimate assumes. It is deliberately NOT the generation
# reserve: that reserve (`GENERATION_SHARE`) is room for the model's OUTPUT and is spent
# whether or not the input estimate was exact. Conflating the two would let one number
# stand in for two unrelated guarantees.
#
# What it guarantees: a conservative guard. It may refuse a turn that would in fact have
# fit (an acceptable failure), and it will not accept a turn whose estimated input already
# exceeds the margin-reduced room. What it does NOT guarantee: safety against an
# arbitrarily dense tokenizer. Text that tokenizes far worse than four characters per token
# - long runs of CJK, for instance - can still defeat a fixed 10 percent margin. That
# residual case is bounded and truthfully classified rather than hidden: the first request
# may reach the endpoint and be rejected (reported as an upstream failure), and a later
# request the growing transcript defeats is caught by the loop's mid-loop reclassification
# in `llm/tools.py`, never silently truncated. The margin is 10 percent because that keeps
# a normal 8,192-token turn comfortably usable (measured: an ordinary code-profile turn
# loses roughly 90 tokens of headroom) while covering the common JSON/code density gap.
CONTEXT_SAFETY_MARGIN = 0.10


def input_ceiling(
    context_window: int, generation: int, *, margin: float = CONTEXT_SAFETY_MARGIN
) -> int:
    """Tokens available for prompt material (messages plus tool schema) under the margin.

    The generation reserve is taken off the window first and never lent to the prompt; the
    margin is then applied to what remains, so the inequality the agent enforces is
    `(messages + tools) * (1 + margin) <= context_window - generation`. Rearranged, the
    prompt material must fit `(context_window - generation) / (1 + margin)`, which is what
    this returns. A `margin` of zero returns `context_window - generation` unchanged, which
    is why a `ContextBudget` with no margin behaves exactly as it did before the margin
    existed.
    """
    room = max(0, context_window - generation)
    return int(room / (1.0 + margin))


@dataclass(frozen=True)
class TurnBudget:
    """One turn's context window split into tokens per bucket."""

    generation: int
    system: int
    history: int
    retrieval: int


def plan_budget(context_window: int) -> TurnBudget:
    """Split a context window into the four buckets of the Stage 7 table.

    The generation reserve is taken off the top and never lent out, so the three prompt
    buckets together are all the prompt can ever occupy. For an 8192 window that is
    2048 reserved and 6144 for system, history, and retrieval.
    """
    return TurnBudget(
        generation=round(context_window * GENERATION_SHARE),
        system=round(context_window * SYSTEM_SHARE),
        history=round(context_window * HISTORY_SHARE),
        retrieval=round(context_window * RETRIEVAL_SHARE),
    )


@dataclass(frozen=True)
class HistoryMessage:
    """The immutable part of a persisted message that can enter a prompt."""

    role: Literal["user", "assistant"]
    content: str


def trim_history(
    messages: list[dict[str, object]], budget_tokens: int
) -> tuple[list[dict[str, object]], int]:
    """Keep the newest messages that fit, dropping oldest first.

    At least `MINIMUM_HISTORY_MESSAGES` are always kept when that many exist, even when
    they overrun `budget_tokens`: the latest exchange is non-negotiable, and the fit
    check charges that pair up front so keeping it can never push an accepted turn past
    the window.

    Returns:
        The kept messages in chronological order, and the tokens they cost.
    """
    kept: list[dict[str, object]] = []
    used = 0
    for message in reversed(messages):
        cost = estimate_tokens(str(message["content"]))
        if used + cost > budget_tokens and len(kept) >= MINIMUM_HISTORY_MESSAGES:
            break
        used += cost
        kept.append(message)
    kept.reverse()
    return kept, used


def mandatory_history_tokens(history: Sequence[HistoryMessage]) -> int:
    """The cost of the newest messages `trim_history` keeps whatever the budget.

    `trim_history` retains at least `MINIMUM_HISTORY_MESSAGES`, so the newest that many
    messages are as non-negotiable as the current message: their cost is charged up front
    rather than left to overflow the window after the optional buckets have already been
    clamped to nothing. Fewer than that many candidates charge only what exists.
    """
    kept = history[-MINIMUM_HISTORY_MESSAGES:] if MINIMUM_HISTORY_MESSAGES else []
    return sum(estimate_tokens(message.content) for message in kept)


@dataclass(frozen=True)
class TurnReserve:
    """The non-trimmable cost of one turn, and whether it fits a window.

    Tutor, writer, and agent turns each assemble different fixed prompt material, but the
    inequality that decides whether a turn can run at all is the same: the generation
    reserve, the fixed prompt material, the current message, and the newest history that
    trimming may never drop must together fit the configured window. Defining that
    inequality once, here, is what keeps a preflight refusal and the request it guards
    from measuring the turn two different ways.

    Attributes:
        context_window: The endpoint's configured window, the ceiling the turn must fit.
        generation: Tokens reserved for the model's own reply, never lent to the prompt.
        fixed_tokens: Non-trimmable prompt material other than history and the current
            message - the system prompt for a tutor turn, plus the tool-definition
            overhead for an agent turn, plus any pinned context.
        question_tokens: The current message, appended to every turn and never trimmed.
        mandatory_history_tokens: The newest history `trim_history` keeps regardless of
            budget, charged in full.
    """

    context_window: int
    generation: int
    fixed_tokens: int
    question_tokens: int
    mandatory_history_tokens: int

    @property
    def reserved(self) -> int:
        """Every token the turn cannot avoid spending, measured against the window."""
        return (
            self.generation
            + self.fixed_tokens
            + self.question_tokens
            + self.mandatory_history_tokens
        )

    @property
    def prompt_room(self) -> int:
        """Window left for trimmable material once the reserve, fixed material, and the
        current message are set aside.

        Non-negative exactly when the turn fits, and then it is the room the mandatory
        history and everything optional after it must share. The mandatory history is not
        subtracted here: it is part of the room, charged against it first.
        """
        return self.context_window - self.generation - self.fixed_tokens - self.question_tokens

    @property
    def fits(self) -> bool:
        """Whether the reserve plus all non-trimmable material fits the window."""
        return self.reserved <= self.context_window

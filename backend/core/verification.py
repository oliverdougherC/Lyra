"""Checking a finished solution with tools, and turning what happened into a verdict.

The single highest-value check available for the mathematics and engineering work Lyra is
best at, and the reason the tool-calling loop exists in Phase 2 rather than in Phase 4.

**It is a separate pass over work that is already written.** Solving ran without tools;
this runs with them. That keeps solving working against an endpoint that does not
implement `tools` at all, and it keeps the check independent of the work, which is the
whole reason a check is worth anything.

The rules this module exists to hold, in the order they matter:

1. **Nothing that is not a check renders as a pass.** A loop that timed out, an endpoint
   with no tool support, and a solution nobody could check mechanically are three
   different outcomes and none of them is `verified`.
2. **`verified` requires that a tool actually ran.** A model that answers "looks right"
   without calling anything has ratified its own work, which is exactly what deterministic
   checking exists to avoid.
3. **A stated disagreement is never softened.** If the checker says a step is wrong, that
   is `refuted` and the student is told, whether or not a tool settled it.
4. **Every verdict says why it is that verdict.** `uncheckable` in particular is not a
   complaint and not a failure — most of it is a solution whose steps are prose — so it
   carries the reason it could not be settled by calculation, ahead of whatever the checker
   said about the work. Without that, a student meets "Nothing to check" sitting above a
   paragraph explaining why every step is right and has no way to tell which to believe.
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from backend.core import artifacts
from backend.core.app_settings import TutorConfig
from backend.core.artifacts import CheckEntry
from backend.llm import replies, tools
from backend.llm.prompts import build_verification_prompt

logger = logging.getLogger(__name__)

AGREES = "agrees"
DISAGREES = "disagrees"
NOTHING_TO_CHECK = "nothing_to_check"

NO_TOOL_SUPPORT_DETAIL = (
    "Your model endpoint cannot run the checks Lyra verifies with, so this solution was "
    "not checked."
)
NO_CHECKS_DETAIL = (
    "Nothing here could be settled by a calculation, so Lyra read the working through "
    "instead and found nothing to disagree with."
)
CHECKS_FAILED_DETAIL = (
    "There was something here to settle, but every calculation Lyra ran failed, so this "
    "solution has not been checked."
)
UNREADABLE_DETAIL = "The check did not report a result Lyra could read."
REFUTED_FALLBACK = "A check disagreed with this solution."


@dataclass(frozen=True)
class VerificationOutcome:
    """What one verification pass concluded, and the calls it made getting there.

    Attributes:
        verdict: One of `artifacts.VERDICTS`.
        detail: The sentence behind the verdict, written for the student.
        checks: Every tool call in the order it ran, including refused and failed ones.
            Kept even on an incomplete pass: partial work is worth showing, it is just not
            worth reading as an answer.
    """

    verdict: str
    detail: str
    checks: tuple[CheckEntry, ...] = ()

    @property
    def refuted(self) -> bool:
        """Whether a check contradicted the solution, which is what triggers a re-derive."""
        return self.verdict == artifacts.REFUTED


def _strip_code_fence(content: str) -> str:
    """Remove one wrapping markdown fence, tagged or bare."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _embedded_report(text: str) -> Mapping[str, object] | None:
    """The report object inside a reply that carries prose around it, or None.

    Searched from the last opening brace backwards, because a checker that talks either
    side of its JSON puts the report at the end far more often than at the start, and an
    object that does not carry a `verdict` is not the report whatever else it holds.
    """
    for start in reversed([index for index, char in enumerate(text) if char == "{"]):
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth:
                    continue
                candidate = replies.loads(text[start : index + 1])
                if isinstance(candidate, Mapping) and "verdict" in candidate:
                    return candidate
                break
    return None


def read_report(content: str) -> tuple[str, str]:
    """Read the checker's closing message into `(verdict word, detail)`.

    Tolerant on purpose. A checker that wrapped its JSON in a fence, or answered in prose
    that plainly says it disagreed, has still told us something, and discarding that on a
    formatting technicality would turn a caught error into a silent pass.

    Returns:
        One of `agrees`, `disagrees`, `nothing_to_check`, or an empty string when the
        reply says nothing usable.
    """
    stripped = _strip_code_fence(content)
    # Read through `replies`, so a report whose detail says what it checked in the notation
    # it checked it in — "the angular frequency $\omega$ is correct" — is a report and not
    # a formatting error. A bare `json.loads` refuses that outright, and the check it threw
    # away had run, called its tools, and agreed.
    payload: object | None = replies.loads(stripped)
    if payload is None:
        # A checker that narrated a sentence before its JSON, or added one after it, has
        # still reported a verdict. Measured against a real signals set, one problem in
        # ten came back this way and its verdict was thrown away as unreadable, which
        # loses a check that actually ran.
        payload = _embedded_report(stripped)

    if isinstance(payload, Mapping):
        word = str(payload.get("verdict") or "").strip().lower()
        detail = str(payload.get("detail") or "").strip()
        if word in (AGREES, DISAGREES, NOTHING_TO_CHECK):
            return word, detail

    # No usable JSON. Only a plainly stated disagreement is read out of prose: reading
    # agreement out of prose would let a reply that never checked anything become a pass,
    # which is the one mistake this module cannot make.
    lowered = stripped.lower()
    if DISAGREES in lowered or '"verdict": "disagrees"' in lowered:
        return DISAGREES, stripped[:400]
    return "", stripped[:400]


def _as_checks(calls: tuple[tools.RecordedCall, ...]) -> tuple[CheckEntry, ...]:
    """Turn the loop's transcript into rows for the audit trail."""
    return tuple(
        CheckEntry(
            tool=call.name,
            arguments=json.dumps(call.arguments, separators=(",", ":")),
            ok=call.ok,
            result=json.dumps(call.result, separators=(",", ":")),
        )
        for call in calls
    )


def judge(result: tools.ToolLoopResult) -> VerificationOutcome:
    """Turn one finished tool loop into a verdict.

    Split out from `verify` so the rules above are testable without a transport: this is
    where "never a pass unless a tool ran" actually lives.
    """
    checks = _as_checks(result.calls)
    if not result.complete:
        # Depth, timeout, an unreachable endpoint, or no tool support. All of them mean
        # the check did not finish, and none of them means the solution is fine.
        detail = result.detail or "Checking did not finish."
        if result.stopped == tools.NO_TOOL_SUPPORT:
            detail = NO_TOOL_SUPPORT_DETAIL
        return VerificationOutcome(artifacts.UNCHECKED, detail, checks)

    word, detail = read_report(result.content)

    if word == DISAGREES:
        return VerificationOutcome(artifacts.REFUTED, detail or REFUTED_FALLBACK, checks)

    if not word:
        # The closing message said nothing readable. Reporting that as agreement would be
        # inventing a conclusion nobody reached, and it is settled here — before anything
        # below reads `detail` as the checker's opinion — because that text is not an
        # opinion, it is whatever the reply happened to contain. What it did say is carried
        # through: a student looking at twenty-five checks and a "not checked" badge is owed
        # the checker's own words, and without them this outcome cannot be diagnosed at all,
        # because nothing keeps the reply.
        said = detail.strip().replace("\n", " ")[:200]
        return VerificationOutcome(
            artifacts.UNCHECKED,
            f"{UNREADABLE_DETAIL} It said: {said}" if said else UNREADABLE_DETAIL,
            checks,
        )

    if not any(check.ok for check in checks):
        if checks:
            # Calls were made and every one of them failed. Something here was worth
            # settling and settling it did not work, which is a check that did not happen
            # rather than a solution with nothing in it to check — and it must not be shown
            # as one, because the difference is the whole reason the badge is trusted.
            return VerificationOutcome(artifacts.UNCHECKED, CHECKS_FAILED_DETAIL, checks)
        # Nothing was called at all. The checker read the work and had nothing to compute
        # about it, whether it said so outright or simply agreed — and either way no
        # calculation settled any of this, so it is not `verified`.
        #
        # Its own sentence is kept behind the reason rather than instead of it. A student
        # met by "Nothing to check" above a paragraph explaining why every step is correct
        # is owed the sentence that reconciles the two, and that sentence is not the
        # checker's: it is why what the checker said is not a check.
        said = detail.strip()
        return VerificationOutcome(
            artifacts.UNCHECKABLE,
            f"{NO_CHECKS_DETAIL} It said: {said}" if said else NO_CHECKS_DETAIL,
            checks,
        )

    if word == NOTHING_TO_CHECK:
        return VerificationOutcome(artifacts.UNCHECKABLE, detail or NO_CHECKS_DETAIL, checks)
    return VerificationOutcome(artifacts.VERIFIED, detail, checks)


def verify(
    config: TutorConfig,
    statement: str,
    label: str,
    solution: str,
    *,
    refutation: str = "",
) -> VerificationOutcome:
    """Check one finished solution and report what happened.

    Args:
        config: Tutor endpoint configuration.
        statement: The problem as confirmed at the review gate.
        label: What the sheet calls it.
        solution: The written solution, steps and answer together.
        refutation: What a previous check concluded, present only on the second pass over
            a re-derived solution.

    Returns:
        The verdict, its detail, and every tool call made. Never raises: a check that
        could not run is a check that did not run, not an error the student needs to see
        in place of their solution.
    """
    messages = build_verification_prompt(statement, label, solution, refutation=refutation)
    try:
        # The solve worker is a plain thread with no event loop, and the loop is async.
        # Owning a loop for the length of the pass keeps this function synchronous.
        result = asyncio.run(
            tools.run_tool_loop(config.endpoint_url, config.api_key, config.model, messages)
        )
    except Exception:
        logger.exception("Verification could not run for %s", label)
        return VerificationOutcome(artifacts.UNCHECKED, "Checking could not be run.")
    return judge(result)

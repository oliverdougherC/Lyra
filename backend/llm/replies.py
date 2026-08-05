"""Reading JSON back out of a reply written by a model.

A model doing mathematics writes mathematics, and mathematics is backslashes: `$\\omega$`,
`\\frac{1}{2}`, `\\theta`. Inside a JSON string a backslash is an escape character, and that
breaks such a reply in two different ways.

1. **It is refused.** `\\omega` is not a valid escape, so `json.loads` rejects the whole
   reply — the right keys, the right verdict, the work actually done, all of it. That is
   how a solution that had been checked and agreed with reached the student as "the check
   did not report a result Lyra could read", quoting the very report it had refused to read.
2. **It is silently mangled.** `\\f`, `\\b`, `\\n`, `\\r` and `\\t` *are* valid escapes, so
   `\\frac{1}{2}` parses without complaint into a form feed and `rac{1}{2}`, and `\\theta`
   into a tab and `heta`. This is the worse of the two: nothing anywhere reports it, and
   the student is shown mathematics with its names eaten.

The five ambiguous letters are settled here as follows. `\\f` and `\\b` are always LaTeX: a
form feed or a backspace is not something a model means to put in a sentence. `\\n`, `\\r`
and `\\t` are whitespace a model does use deliberately, so they stay escapes unless what
follows spells a LaTeX command — which is what keeps `\\theta`, `\\tau`, `\\rho` and `\\to`
intact while `"one\\ntwo"` still breaks its line. A command not on that list falls back to
the whitespace reading, which is what this module did before it existed.

The repair is not a JSON parser of our own. It is one pass that doubles the backslashes a
model meant literally, leaving real escapes alone, and hands the result back to
`json.loads`. Nothing is guessed: a reply that is not JSON after that comes back as None.
"""

import json
import re

# LaTeX commands beginning with `n`, `r` or `t`, matched as prefixes: `right` covers
# `rightarrow`, `ne` covers `neq`. Only these three letters need a list — every other
# command either starts with a letter that was never an escape, or starts with `f` or `b`,
# which are read as LaTeX outright.
_NRT_COMMANDS = (
    "nabla", "ne", "newline", "ngeq", "ni", "nleq", "nmid", "nolimits", "nonumber", "not",
    "nsim", "nu",
    "rangle", "rbrace", "rbrack", "rceil", "real", "rfloor", "rho", "right", "rm", "root",
    "rvert",
    "tan", "tau", "tbinom", "text", "tfrac", "therefore", "theta", "tilde", "times", "to",
    "triangle",
)  # fmt: skip

# One escape, one backslash-and-letters that may be a command, or one bare backslash. A
# single alternation so a real escape is consumed whole: scanning backslash by backslash,
# the second half of a legitimate `\\` looks exactly like a stray one and would be doubled.
_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/])|\\([a-zA-Z]+)|\\')


def _is_whitespace_escape(run: str) -> bool:
    """Whether `\\` + `run` reads better as the whitespace a model asked for than as LaTeX."""
    return run[0] in "nrt" and not any(run.startswith(command) for command in _NRT_COMMANDS)


def _double_stray_backslashes(text: str) -> str:
    """Escape the backslashes a model wrote as themselves rather than as escapes."""

    def repair(match: re.Match[str]) -> str:
        run = match.group(1)
        if run is None:
            # A real escape (kept), or a backslash before punctuation (doubled).
            return match.group(0) if len(match.group(0)) > 1 else "\\\\"
        return match.group(0) if _is_whitespace_escape(run) else f"\\{match.group(0)}"

    return _ESCAPE.sub(repair, text)


def strip_code_fence(content: str) -> str:
    """Remove one wrapping markdown fence, tagged (```json) or bare (```)."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def loads_object(content: str) -> dict[str, object] | None:
    """Read a reply that was asked for as a JSON object, fence and all.

    Returns:
        The parsed object, or None when the reply is not JSON or is JSON of another shape.
        A prompt that asked for an object and got a list has not been answered, and the
        caller has no more to go on than if nothing had parsed at all.
    """
    payload = loads(strip_code_fence(content))
    return payload if isinstance(payload, dict) else None


def loads(text: str) -> object | None:
    """Read `text` as JSON, tolerating the way models write it.

    Returns:
        The parsed value, or None when the reply cannot be read as JSON at all.
    """
    repaired = _double_stray_backslashes(text)
    try:
        # `strict=False` as well, which allows the raw newlines and tabs a model drops into
        # a string it is writing prose into. Same principle: a formatting difference is not
        # a missing answer.
        return json.loads(repaired, strict=False)
    except ValueError:
        pass
    if repaired == text:
        return None
    # The repair changed something and the result does not parse, so the reply is malformed
    # in some other way as well. Its own text still gets a reading, in case that one lands.
    try:
        return json.loads(text)
    except ValueError:
        return None

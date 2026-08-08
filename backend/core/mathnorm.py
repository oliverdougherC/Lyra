"""Math delimiters, normalized to the ones the whole stack agrees on.

Everything downstream of a draft body reads `$...$` and `$$...$$`: the editor's
remark-math parser, Pandoc's markdown reader on the way to Typst, and the chat renderer.
Models do not reliably write those. They write `\\(x\\)` and `\\[ ... \\]` (LaTeX's own
delimiters, and what a model trained on LaTeX reaches for first), and they write bare
`\\begin{align}` blocks with no delimiter at all. All three render as literal backslashes
and braces in the editor and come out of the PDF the same way - which is most of "the
LaTeX formatting doesn't work" for a technical draft.

So AI-written text is normalized where it lands, once, and the stored body converges on
one convention. The frontend has the same conversion for bodies written before this
existed (`normalizeMathDelimiters` in `chat/markdown-utils.ts`); this is the copy that
keeps new text from needing it.

Two things are deliberately left alone:

- `\\[TODO: ...]`. The Milkdown editor serializes `[TODO:` as `\\[TODO:` because a bare
  bracket could open a link, so a document that has been through the editor is full of
  what looks like an opening display delimiter. The section index tolerates the escape
  (`sections._TODO_LINE`); turning one into `$$TODO: ...$$` would break the emptiness
  rule the whole drafting pipeline is built on.
- `\\$`. An escaped dollar is a dollar sign the writer wanted - a price, not math.
"""

import re

# The environments worth promoting to display math, matching the frontend's list. An
# `\begin{align}` that a model wrote bare is display math whatever it forgot to type
# around it.
_DISPLAY_ENVIRONMENTS = (
    "equation",
    "align",
    "gather",
    "multline",
    "cases",
    "dcases",
    "aligned",
    "split",
    "matrix",
    "pmatrix",
    "bmatrix",
)

_ENVIRONMENT_NAMES = "|".join(f"{name}\\*?" for name in _DISPLAY_ENVIRONMENTS)

# A bare environment block standing alone on its own lines: promote the whole block. Only
# reached for text outside an existing `$$` block - see `_split_verbatim`, without which
# this rule wraps an already-delimited environment a second time on every load.
_BARE_ENVIRONMENT = re.compile(
    r"(?<!\\)^([ \t]*)"
    rf"(\\begin\{{(?:{_ENVIRONMENT_NAMES})\}}.*?\\end\{{(?:{_ENVIRONMENT_NAMES})\}})"
    r"[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

# `\(...\)` and `\[...\]`, non-greedy so adjacent spans do not merge into one. The TODO
# guard is a negative lookahead rather than a post-filter: `\[TODO: cite]` must not even
# be considered a delimiter, because its closing `]` is not a `\]` and a greedy reading
# would swallow the rest of the document looking for one.
_INLINE_PAREN = re.compile(r"(?<!\\)\\\((.+?)(?<!\\)\\\)", re.DOTALL)
_DISPLAY_BRACKET = re.compile(r"(?<!\\)\\\[(?!\s*TODO\b)(.+?)(?<!\\)\\\]", re.DOTALL)

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INDENTED_CODE = re.compile(r"(?m)^(?: {4,})(?=\S)")
_LEADING_SPACE_ENTITY = re.compile(r"(?m)^(?:(?:&#x20;|&#32;|&nbsp;))+")
_UNDLIMITED_COMMAND = re.compile(
    r"(?<![\\\w])"
    r"(\\[A-Za-zα-ωΑ-Ω]+\*?(?:\{[^{}]*\}|\[[^\]]*\])*(?:\\?[_^](?:\{[^{}]*\}|\\[A-Za-z]+|[A-Za-z0-9]+))*)"
    r"(?=$|[\s.,;:!?\\)])"
)
_UNDLIMITED_VARIABLE = re.compile(
    r"(?<![\\\w])"
    r"(?:[A-Za-zα-ωΑ-Ω](?:\\?[_^](?:\{[^{}]*\}|[A-Za-z0-9]+|\\[A-Za-z]+))+)"
    r"(?=$|[\s.,;:!?\\)])"
)


def normalize(text: str) -> str:
    """Rewrite LaTeX-style math delimiters to the `$` forms the stack reads.

    Idempotent: text that is already normalized comes back unchanged, which matters
    because a body is normalized at every landing and re-normalized on the way into the
    editor.
    """
    if not text or (
        "\\" not in text
        and not _INDENTED_CODE.search(text)
        and not _LEADING_SPACE_ENTITY.search(text)
        and not _UNDLIMITED_VARIABLE.search(text)
        and not _UNDLIMITED_COMMAND.search(text)
    ):
        return text
    return "".join(
        segment if verbatim else _normalize_prose(segment)
        for segment, verbatim in _split_verbatim(text)
    )


def _normalize_prose(text: str) -> str:
    """The conversions, applied to one run of non-code text."""
    text = _INLINE_PAREN.sub(lambda match: f"${match.group(1).strip()}$", text)
    text = _DISPLAY_BRACKET.sub(lambda match: f"\n$$\n{match.group(1).strip()}\n$$\n", text)
    text = _BARE_ENVIRONMENT.sub(lambda match: f"{match.group(1)}$$\n{match.group(2)}\n$$", text)
    text = _repair_accidental_indentation(text)
    # The conversions above can create fresh display blocks. Split once more so the
    # token repair does not wrap commands *inside* the math we just delimited.
    return "".join(
        segment if verbatim else _wrap_undelimited_math(segment)
        for segment, verbatim in _split_verbatim(text)
    )


def _repair_accidental_indentation(text: str) -> str:
    """Repair prose accidentally serialized as indented code blocks."""
    lines = text.splitlines(keepends=True)
    repaired: list[str] = []
    for line in lines:
        entity_match = _LEADING_SPACE_ENTITY.match(line)
        if entity_match is not None:
            raw = entity_match.group(0)
            line = " " * (len(raw) // 6) + line[entity_match.end() :]
        leading_spaces = len(line) - len(line.lstrip(" "))
        if leading_spaces >= 4:
            stripped = line.lstrip(" ")
            prose = stripped.rstrip("\r\n")
            if (
                prose
                and prose[0] not in "-+*>|#`~"
                and len(prose) >= 100
                and len(prose.split()) >= 12
            ):
                line = stripped
        repaired.append(line)
    return "".join(repaired)


def _wrap_undelimited_math(text: str) -> str:
    """Wrap non-delimited LaTeX-ish symbol runs so export/editor parse as math."""

    return "".join(
        line if line.startswith(("    ", "\t")) else _wrap_undelimited_math_run(line)
        for line in text.splitlines(keepends=True)
    )


def _wrap_undelimited_math_run(text: str) -> str:
    """Wrap one non-code run while preserving existing dollar-delimited math."""

    def wrap_in_text(chunk: str) -> str:
        chunk = _UNDLIMITED_COMMAND.sub(
            lambda match: f"${_unescape_math_script(match.group(1))}$", chunk
        )
        return _UNDLIMITED_VARIABLE.sub(
            lambda match: f"${_unescape_math_script(match.group(0))}$", chunk
        )

    segments: list[str] = []
    prose_start = 0
    cursor = 0
    while cursor < len(text):
        if text[cursor] != "$" or _is_escaped(text, cursor):
            cursor += 1
            continue
        delimiter = "$$" if text.startswith("$$", cursor) else "$"
        close = _find_dollar_close(text, cursor + len(delimiter), delimiter)
        if close < 0:
            break
        segments.append(wrap_in_text(text[prose_start:cursor]))
        segments.append(text[cursor : close + len(delimiter)])
        cursor = close + len(delimiter)
        prose_start = cursor
    segments.append(wrap_in_text(text[prose_start:]))
    return "".join(segments)


def _unescape_math_script(token: str) -> str:
    """Undo Markdown's escape for a sub/superscript once it becomes math."""
    return token.replace(r"\_", "_").replace(r"\^", "^")


def _is_escaped(text: str, index: int) -> bool:
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _find_dollar_close(text: str, start: int, delimiter: str) -> int:
    cursor = start
    while cursor < len(text):
        if text.startswith(delimiter, cursor) and not _is_escaped(text, cursor):
            return cursor
        cursor += 1
    return -1


def _split_verbatim(text: str) -> list[tuple[str, bool]]:
    """The text as runs, each flagged as "leave this alone".

    Two kinds are left alone. A fenced code block is verbatim by definition: a shell
    snippet holding `\\(` is not math, and a LaTeX example block is showing the delimiters
    rather than using them. A `$$` block is already display math, and running the
    environment rule inside one wraps it a second time - which, because bodies are
    normalized on every load, nests one more `$$` pair around the same equation every time
    the draft is opened.
    """
    runs: list[tuple[str, bool]] = []
    buffer: list[str] = []
    marker: str | None = None
    in_math = False
    for line in text.splitlines(keepends=True):
        fence = _FENCE.match(line)
        is_math_delimiter = marker is None and line.strip() == "$$"
        if is_math_delimiter:
            if in_math:
                buffer.append(line)
                runs.append(("".join(buffer), True))
                buffer = []
                in_math = False
            else:
                runs.append(("".join(buffer), False))
                buffer = [line]
                in_math = True
        elif in_math:
            buffer.append(line)
        elif marker is None and fence:
            runs.append(("".join(buffer), False))
            buffer = [line]
            marker = fence.group(1)
        elif marker is not None and fence and fence.group(1)[0] == marker[0]:
            buffer.append(line)
            runs.append(("".join(buffer), True))
            buffer = []
            marker = None
        else:
            buffer.append(line)
    if buffer:
        runs.append(("".join(buffer), marker is not None or in_math))
    return runs

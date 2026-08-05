"""Contract tests for reading JSON out of a reply a model wrote.

The rule the module exists to hold: mathematics survives the trip. A model asked for JSON
answers in the notation it was reading, and a backslash means one thing to LaTeX and
another to JSON. Both readings of that collision lose the student's work — one refuses the
reply outright, the other eats the name of the command and says nothing.
"""

import pytest

from backend.llm import replies


def test_a_report_carrying_latex_is_read_rather_than_refused() -> None:
    """The failure this module was written for, in the words it actually arrived in.

    `\\omega` is not a valid JSON escape, so a strict read throws away a check that ran,
    called its tools, and agreed — and then quotes the report it refused to read.
    """
    payload = replies.loads(
        '{"verdict": "agrees", "detail": "The angular frequency $\\omega$ is correct."}'
    )

    assert payload == {"verdict": "agrees", "detail": "The angular frequency $\\omega$ is correct."}


@pytest.mark.parametrize(
    "command",
    [
        # The five escapes that are also the start of a command. `\f` and `\b` parse
        # cleanly into a form feed and a backspace; the other three into whitespace. None
        # of them raises, so without this every one is a silent corruption.
        "\\frac{1}{2}",
        "\\beta",
        "\\begin{cases}",
        "\\theta",
        "\\tau",
        "\\times",
        "\\to",
        "\\rho",
        "\\rightarrow",
        "\\nu",
        "\\neq",
        # And one that is refused outright rather than mangled.
        "\\omega",
        "\\underbrace{x}",
    ],
)
def test_a_command_survives_being_written_into_json(command: str) -> None:
    payload = replies.loads(f'{{"answer": "${command}$"}}')

    assert payload == {"answer": f"${command}$"}


def test_an_escape_a_model_meant_is_still_an_escape() -> None:
    """The other side of the trade. A reply that meant a line break gets its line break."""
    payload = replies.loads(
        '{"a": "one\\ntwo", "b": "tab\\there", "c": "C:\\\\path", "d": "\\u00e9"}'
    )

    assert payload == {"a": "one\ntwo", "b": "tab\there", "c": "C:\\path", "d": "é"}


def test_a_raw_newline_inside_a_string_is_read() -> None:
    # Strictly invalid JSON, and a shape models produce constantly when writing prose.
    assert replies.loads('{"a": "line\nbreak"}') == {"a": "line\nbreak"}


@pytest.mark.parametrize("text", ["", "not json at all", "{unclosed", "```json"])
def test_a_reply_that_is_not_json_reads_as_nothing(text: str) -> None:
    # Nothing is guessed. A reply that cannot be read is reported as unreadable, which is
    # what keeps a repair from inventing a conclusion nobody reached.
    assert replies.loads(text) is None

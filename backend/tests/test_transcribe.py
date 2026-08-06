"""Guards for reading a page as an image.

The interesting behaviour here is what transcription refuses to do. It sends a picture of
the student's own document somewhere, so the locality rule is not a formality, and a model
told to transcribe will still wrap its answer in a fence it was told not to add.
"""

import httpx
import pytest

from backend.core.errors import UpstreamError
from backend.rag import transcribe

_LOCAL = "http://127.0.0.1:8080/v1"
_REMOTE = "https://api.example.com/v1"

_PAGE = b"\x89PNG not really an image"


def _replying(text: str) -> httpx.MockTransport:
    """A stubbed endpoint answering one completion with fixed content."""
    return httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": text}}]})
    )


async def test_a_page_is_sent_with_the_transcription_prompt() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "78 Matrices"}}]})

    text = await transcribe.transcribe_page(
        _LOCAL, None, "vision", _PAGE, transport=httpx.MockTransport(handler)
    )

    assert text == "78 Matrices"
    parts = sent[0]["messages"][0]["content"]
    assert parts[0]["text"] == transcribe.TRANSCRIBE_PROMPT
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_a_remote_endpoint_without_acknowledgement_is_refused() -> None:
    """The rule `extract_facts` already follows, and it matters more here.

    What travels is a picture of the student's document rather than a paragraph of its
    text, so this refuses before the image is encoded rather than after.
    """
    with pytest.raises(UpstreamError) as caught:
        await transcribe.transcribe_page(
            _REMOTE, None, "vision", _PAGE, transport=_replying("anything")
        )

    assert "not on this machine" in caught.value.message


async def test_a_remote_endpoint_the_student_acknowledged_is_allowed() -> None:
    text = await transcribe.transcribe_page(
        _REMOTE, None, "vision", _PAGE, remote_ack=True, transport=_replying("page text")
    )

    assert text == "page text"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("```markdown\n78 Matrices\n```", "78 Matrices"),
        ("```latex\n$x^2$\n```", "$x^2$"),
        ("```\nplain\n```", "plain"),
        # Not a wrapper: the page's own content happens to open with a fence.
        (
            "```python\nprint(1)\n```\nand then some prose",
            "```python\nprint(1)\n```\nand then some prose",
        ),
        ("no fence at all", "no fence at all"),
    ],
)
async def test_a_fence_wrapped_around_the_whole_page_is_not_part_of_the_page(
    reply: str, expected: str
) -> None:
    """Models fence a transcription even when told to reply with the transcription alone.

    Left in, three backticks reach the chunker as if the page contained a code block.
    """
    assert (
        await transcribe.transcribe_page(_LOCAL, None, "vision", _PAGE, transport=_replying(reply))
        == expected
    )


async def test_a_blank_page_transcribes_to_nothing_rather_than_failing() -> None:
    """An empty answer is the correct reading of an empty page, not an error."""
    assert (
        await transcribe.transcribe_page(_LOCAL, None, "vision", _PAGE, transport=_replying(""))
        == ""
    )


async def test_a_truncated_reply_fails_the_page_rather_than_being_stored() -> None:
    """`finish_reason: "length"` on a transcription is half a page, not a page.

    Stored, it would enter retrieval wearing a whole page's name and nothing downstream
    could ever tell. The rag-pipeline document mandates the ceiling for exactly this: a
    reply still running at the token limit is a repetition loop, and the loop's partial
    output must fail this one page loudly.
    """
    truncated = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "# C.1 Basic Discrete"}, "finish_reason": "length"}
                ]
            },
        )
    )

    with pytest.raises(UpstreamError) as caught:
        await transcribe.transcribe_page(_LOCAL, None, "vision", _PAGE, transport=truncated)

    assert "output-token ceiling" in caught.value.message


async def test_every_page_request_carries_the_output_ceiling() -> None:
    """Without `max_tokens`, a repetition loop is a request that never ends.

    The number mirrors the specialist runtime's own ceiling because it encodes the same
    fact about the same input: no single page transcribes to more than this.
    """
    import json

    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "78 Matrices"}}]})

    await transcribe.transcribe_page(
        _LOCAL, None, "vision", _PAGE, transport=httpx.MockTransport(handler)
    )

    assert sent[0]["max_tokens"] == transcribe.MAX_TRANSCRIPTION_TOKENS


def test_the_prompt_pins_one_notation_for_tables() -> None:
    """Asked only to "transcribe", a model picks a notation per page: measured over one
    eight-page appendix, the same document's tables came back as bare alternating lines, as
    bare pipes, as a full Markdown table, and as a LaTeX `tabular`. Nothing downstream can
    tell a table from a paragraph unless the transcription says which it is."""
    prompt = transcribe.TRANSCRIBE_PROMPT

    assert "Markdown table" in prompt
    assert "| --- |" in prompt
    # Including where the page prints no header, because a header row that is sometimes
    # there and sometimes not is a second notation wearing the first one's clothes.
    assert "no printed header" in prompt
    assert "Never use a LaTeX table" in prompt


def test_the_prompt_pins_one_notation_for_headings() -> None:
    """`chunk.SECTION_HEADING` sees an ATX heading. It does not see a heading split across
    two lines mid-phrase, or one written in bold, and a scanned appendix arrived as both."""
    prompt = transcribe.TRANSCRIBE_PROMPT

    assert "on ONE line, starting with `#`" in prompt
    assert "Never split a heading across" in prompt
    assert "never write one in bold" in prompt

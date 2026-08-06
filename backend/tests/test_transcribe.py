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

"""Transcription: page in, text out.

Stage 2b of docs/rag-pipeline.md. The interface is deliberately narrow, because two
implementations sit behind it and neither should leak into the other. Today it is the
configured vision model. The `Unlimited-OCR` specialist is the same signature with a
different runtime, and whether it earns its multi-gigabyte download is a measurement rather
than a decision taken in advance.

**This is not only for scanned pages.** PyMuPDF's text layer loses mathematics on pages
that were never scanned: on the reference textbook every matrix extracts as a column of
loose digits with its shape discarded, so an identity matrix and a row swap both arrive as
`1 0 0 1` down four lines with nothing to tell them apart. Reading the page as an image is
the only way back to what the page actually says.

Nothing here is wired into ingestion yet. That is Step 4, along with the per-page state a
page that can fail on its own needs.
"""

import logging

from backend.core.errors import UpstreamError
from backend.llm import client
from backend.llm.locality import is_local_endpoint

logger = logging.getLogger(__name__)

# What the model is asked for. Written to get a faithful reading rather than a helpful one:
# a vision model left to itself summarises a page, and a summary of a page of mathematics
# is not a transcription of it.
TRANSCRIBE_PROMPT = """\
Transcribe this page exactly as it appears. Reply with the transcription and nothing else.

Rules:
- Copy every line of text in reading order. Do not summarise, explain, or comment.
- Write mathematics as LaTeX: `$...$` inline and `$$...$$` on its own line. A matrix is a
  matrix, not a list of numbers, so use \\begin{bmatrix} rather than writing its entries
  out in a column.
- Keep headings, numbering, and labels exactly as printed, including section numbers.
- Where a figure or diagram appears, write `[figure]` on its own line and carry on. Do not
  describe it.
- If the page is blank, reply with nothing at all."""

REMOTE_MESSAGE = (
    "Reading pages sends an image of your document to your model endpoint, which is not on "
    "this machine. Acknowledge the endpoint in Settings first."
)
NO_VISION_MESSAGE = (
    "The configured model cannot read images, so this page cannot be transcribed. "
    "Check the connection in Settings."
)


async def transcribe_page(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    image: bytes,
    *,
    remote_ack: bool = False,
    transport: object | None = None,
) -> str:
    """Read one rendered page and return what it says.

    Args:
        endpoint: Tutor endpoint base URL, including its version suffix.
        api_key: Bearer token, or None when the endpoint needs no auth.
        model: Model identifier, omitted from the request when None.
        image: PNG bytes of one page, rendered at `render.RECOGNITION_DPI`.
        remote_ack: Whether the student has acknowledged a non-local endpoint.
        transport: Test seam, passed through to the client.

    Returns:
        The page's text. An empty string is a valid answer for a blank page and must not
        be treated as a failure.

    Raises:
        UpstreamError: The endpoint is not local and has not been acknowledged, or the
            request failed.
    """
    if not is_local_endpoint(endpoint) and not remote_ack:
        # The same rule `extract_facts` follows, and it matters more here: what gets sent
        # is a picture of the student's own document rather than a paragraph of its text.
        raise UpstreamError(REMOTE_MESSAGE)

    message = client.image_message(TRANSCRIBE_PROMPT, image)
    text = await client.complete(endpoint, api_key, model, [message], transport=transport)  # type: ignore[arg-type]
    return _cleaned(text)


def _cleaned(text: str) -> str:
    """Strip a fence the model wrapped the whole page in.

    Models routinely return a transcription inside ```` ```markdown ```` or ```` ```latex ````
    even when told to reply with the transcription alone. The fence is not part of the
    page, and leaving it in would put three backticks into the chunker's input.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    # Only an opening fence on its own first line and a closing fence on the last counts.
    # A page whose content genuinely contains a code block is left alone.
    if lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()

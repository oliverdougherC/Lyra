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
import re

from backend.core.errors import UpstreamError
from backend.llm import client
from backend.llm.locality import is_local_endpoint
from backend.llm.ocr_server import MAX_OUTPUT_TOKENS, OCR_PROMPT, ocr_server

logger = logging.getLogger(__name__)

# What the model is asked for. Written to get a faithful reading rather than a helpful one:
# a vision model left to itself summarises a page, and a summary of a page of mathematics
# is not a transcription of it.
#
# The markup is pinned, and that is the half of this prompt that was learned rather than
# guessed. Asked only to "transcribe", a model picks a notation per page: measured over one
# eight-page scanned appendix, the same document's tables came back as bare alternating
# lines, as bare pipes with no header rule, as a full Markdown table, and as a raw LaTeX
# `tabular` - four notations for one table. Headings varied the same way, arriving bold-
# wrapped, or split across two lines mid-phrase, or plain.
#
# It matters because everything downstream reads text and nothing downstream can tell a
# table from a paragraph unless the transcription says so. `chunk.SECTION_HEADING` sees an
# ATX heading and does not see `C.1 Basic Discrete-Time Fourier Series Pairs` split over
# two lines, so that appendix chunked as eight anonymous pages with none of its nine
# sections named - which is a retrieval failure created entirely at transcription time.
#
# One notation per thing, therefore, stated as a rule rather than an example, and stated
# even where the page has no header to put in the header row: a table whose header row is
# sometimes present and sometimes not is a second notation wearing the first one's clothes.
TRANSCRIBE_PROMPT = """\
Transcribe this page exactly as it appears. Reply with the transcription and nothing else.

Rules:
- Copy every line of text in reading order. Do not summarise, explain, or comment.
- Write mathematics as LaTeX: `$...$` inline and `$$...$$` on its own line. A matrix is a
  matrix, not a list of numbers, so use \\begin{bmatrix} rather than writing its entries
  out in a column.
- Write every heading as a Markdown heading on ONE line, starting with `#`, keeping its
  number: `# C.1 Basic Discrete-Time Fourier Series Pairs`. Never split a heading across
  two lines, and never write one in bold instead.
- Write every table as a Markdown table, always with a header row and always with the
  `| --- |` rule under it. Where the page's table has no printed header, use the empty
  header `|  |  |`. Never use a LaTeX table, and never write the cells as plain lines.
- Where a figure or diagram appears, write `[figure]` on its own line and carry on. Do not
  describe it.
- If the page is blank, reply with nothing at all."""

REMOTE_MESSAGE = (
    "Reading pages sends an image of your document to your model endpoint, which is not on "
    "this machine. Acknowledge the endpoint in Settings first."
)
# What the specialist emits around its output when asked for special tokens.
#
# `<|det|>label [x0, y0, x1, y1]<|/det|>` prefixes each detected region, and the model's
# end-of-sequence token arrives with the rest. Both bar shapes are matched because this
# model family uses the full-width form.
_DETECTION_MARKER = re.compile(r"<[|｜]det[|｜]>.*?<[|｜]/det[|｜]>", re.DOTALL)
_CONTROL_TOKEN = re.compile(r"<[|｜][^<>]*?[|｜]>")

NO_VISION_MESSAGE = (
    "The configured model cannot read images, so this page cannot be transcribed. "
    "Check the connection in Settings."
)


def _strip_special(text: str) -> str:
    """Remove the control tokens the specialist emits and the layout markers it wraps.

    `llama-server` is run with `--special` because this model carries its layout in special
    tokens: without it the `<|det|>` markers vanish and, since the table cell tags are
    special tokens too, a table arrives with its cells fused. The cost of asking for them
    is that the end-of-sequence token arrives too, and the detection markers are geometry
    rather than text.

    The coordinates are dropped rather than kept because nothing downstream reads them yet.
    Chunking, embedding, and citation all work on text, and a chunk carrying
    `<|det|>table [266, 178, 751, 436]<|/det|>` would embed those numbers as if they were
    words. They are recoverable by re-running the page if a later phase wants them.
    """
    # Markers before control tokens, and the order is load-bearing. The other way round,
    # the generic rule eats the `<|det|>` delimiters first and leaves their contents behind
    # as bare text, so every region arrives as `page_number [115, 106, 150, 119]774`.
    without_markers = _DETECTION_MARKER.sub("", text)
    return _CONTROL_TOKEN.sub("", without_markers).strip()


async def transcribe_page_locally(image: bytes, *, transport: object | None = None) -> str:
    """Read one page with the specialist model on this machine.

    The same signature idea as `transcribe_page` minus the endpoint, because there is no
    endpoint: the model runs here. That difference is the point. Nothing leaves the
    machine, so there is no locality rule to apply and no acknowledgement to ask for, and
    the page image of a student's document stays where the student's document is.

    Args:
        image: PNG bytes of one page, rendered at `render.RECOGNITION_DPI`.
        transport: Test seam.

    Returns:
        The page's text, with the model's control and layout tokens removed.

    Raises:
        ConfigurationError: The weights or the runtime are missing.
        UpstreamError: The local server answered with something unusable.
    """
    ocr_server.ensure_running()
    message = client.image_message(OCR_PROMPT, image)
    text = await client.complete(
        f"{ocr_server.base_url}/v1",
        None,
        None,
        [message],  # type: ignore[arg-type]
        transport=transport,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=client.DETERMINISTIC_TEMPERATURE,
        request_timeout=client.BACKGROUND_TIMEOUT,
    )
    return _strip_special(text)


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
    text = await client.complete(
        endpoint,
        api_key,
        model,
        [message],  # type: ignore[arg-type]
        transport=transport,
        temperature=client.DETERMINISTIC_TEMPERATURE,
        request_timeout=client.BACKGROUND_TIMEOUT,
    )
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

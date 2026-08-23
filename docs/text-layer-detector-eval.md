# Unusable text-layer detection (PLA-148)

Lyra already has a page-selective recognition substrate: `backend/rag/parse.py` decides,
per page, whether the extracted text is worth indexing, and `backend/core/recognition.py`
recognizes only the pages it drops. PLA-148 broadens the *decision*, not the substrate.

## The three page-quality gates

`parse.page_skip_reason(text, photographed)` is the single place that decides a page is
unusable and should join the recognition flow instead of the index. It returns a bounded,
privacy-safe reason code, never the page's text:

| Reason | Rule | Since |
| --- | --- | --- |
| `sparse` | Fewer than `SCANNED_PAGE_MIN_CHARS` non-whitespace characters. A page with no real text layer. | Phase 1 |
| `photographed` | Almost no alphabetic text after URLs are stripped **and** one raster image covers most of the page. A picture wearing a scrap of text. | Phase 3 |
| `character_soup` | Enough word-shaped alphabetic tokens to be claiming to be prose, but the fraction that read as real words is below `_MIN_WORD_PLAUSIBILITY`. OCR gibberish. | **PLA-148** |
| `repetition` | One line, or one token of three or more characters, dominates the page. A broken extraction that repeats a header, footer, or token. | **PLA-148** |

The first two rules and their known-good behavior are pinned by regression tests in
`backend/tests/test_parse.py`; PLA-148 adds no new behavior to them.

## Why these two signals, and not more

The detector is tuned for **precision over recall**. A false positive re-recognizes a page
that already read - minutes of model time, and against a remote endpoint a page image of the
student's own material leaving the machine - so a valid sparse title page, a matrix of lone
digits, a page of code, an equation page, and a table of numbers must all stay readable.
`character_soup` only judges pages with enough word-shaped tokens to be making a claim to be
prose, so letter-light equation and matrix pages are never reached; `repetition` ignores
tokens shorter than three characters, so a matrix where `0` recurs cannot trip it.

Pathologies that cannot be separated from valid pages using the extracted text alone are
**not** chased, because a rule aggressive enough to catch them starts dropping valid pages.
They are labelled in the corpus as known false negatives and reported as such:

- **reordered columns** - the words are all real, only their order is wrong;
- **coherent unrelated overlay** - reads exactly like valid prose;
- **ligature-substitution soup** (`rn`->`m`, `li`->`h`) - produces pronounceable pseudo-words that pass a vowel test without a dictionary;
- **scattered equation glyphs** - indistinguishable from a legitimate equation page.

## Measured results

Scored by `scripts/eval_text_layer.py` against the versioned corpus
`scripts/eval_corpora/text_layer.json` (v1, 23 labelled pages). The scoring arithmetic and
the corpus thresholds are guarded in CI by `backend/tests/test_text_layer.py`.

```
Detector (all cases):        precision 1.000   recall 0.667   (TP 8  FP 0  FN 4  TN 11)
Detector (catchable cases):  precision 1.000   recall 1.000   (TP 8  FP 0  FN 0  TN 11)

Recall by category:  character_soup 1.000   repetition 1.000   image_only 1.000
                     ligature_soup 0.000   reordered_columns 0.000
                     glyph_fragments 0.000  overlay_coherent 0.000

False positives: none.
False negatives: ligature soup, reordered columns, scattered glyphs, coherent overlay
                 (the four categories marked not-detectable-from-text in the corpus).
```

**Precision is 1.000: no usable page is ever flagged.** Recall over the categories the
signals claim to catch is 1.000; overall recall of 0.667 is dragged down only by the
deliberately-uncaught hard categories above.

### Before / after on the corpus

Extraction - pages whose text reaches the index:

| | pages indexed | junk indexed | good indexed |
| --- | --- | --- | --- |
| before PLA-148 | 21 | 10 | 11 |
| after PLA-148 | 15 | 4 | 11 |

Every good page still reaches the index; six of the ten junk pages no longer do (the
remaining four are the honest false negatives).

Retrieval - probe terms findable in the real chunked index (a lexical proxy: retrieval can
only surface text that was chunked):

| | good probes found | junk probes found |
| --- | --- | --- |
| before PLA-148 | 5 / 5 | 5 / 5 |
| after PLA-148 | 5 / 5 | 1 / 5 |

Retrieval recall is fully preserved; retrieval precision improves sharply as junk stops
being indexable. The one junk probe that survives is the ligature-soup false negative.

## Truthfulness and provenance

A flagged page is dropped exactly as a scanned page is, so it inherits the existing
lifecycle guarantees, verified end to end in `backend/tests/test_recognition.py`:

- only newly flagged pages enter recognition; good extracted pages are never re-read;
- page numbers, chunk/section ordering, and the mix of extracted and recognized pages are stable;
- classification and recognition are idempotent and restartable - a reingest re-reads nothing already recognized and duplicates no content;
- when recognition is disabled, unavailable, remotely unacknowledged, or fails, the document does not claim complete readability: an all-junk file is `unsupported`, and a mixed file whose recognition fails lands `ready` while reporting its failed pages.

## Corpus versioning

`scripts/eval_corpora/text_layer.json` carries a `version` field. Bump it when the labelled
cases change, so a detector change is scored against a named corpus rather than a moving
target.

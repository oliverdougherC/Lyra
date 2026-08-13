# Raster envelope for page rendering

`backend/rag/render.py` bounds the raster size of every page and figure crop before it
asks PyMuPDF's `get_pixmap` to allocate the pixmap. This note records what the bound is,
why those numbers, and how it was measured against Lyra's representative course material
(PLA-170).

## The bound

- `MAX_RASTER_PIXELS = 175_000_000` (175 megapixels)
- `MAX_RASTER_DIMENSION = 30_000` px per side

A `get_pixmap` pixmap is DeviceRGB with no alpha, i.e. **3 bytes per pixel**, so 175 MP is
~525 MB of native buffer. `_raster_within_bounds(rect, dpi)` mirrors `get_pixmap`'s own
sizing (`rect` points scaled by `dpi/72`) and refuses anything past either ceiling, or any
non-finite / non-positive / zero-area geometry, before allocation.

## Why the geometry, not the file, is the risk

`get_pixmap` scales the page rectangle by `dpi/72`. The PDF format permits a MediaBox up to
14400 pt (200 in) a side, so a **compact** file can describe an enormous page: a 14400 pt
page is a few KB, and a 40000x40000 solid PNG is under 2 MB. The 50 MB upload limit cannot
see this, because the file is small and the geometry is not. At the 300 dpi recognition
path a 14400 pt page is a single ~3.6 **billion** pixel (10+ GB) allocation - a memory
spike or process kill before Python-level error handling runs.

## PDF vs. image: the recognition-path asymmetry

PyMuPDF opens a standalone image (`image/png`, `image/jpeg`) as a one-page document whose
page rectangle is the **decoded pixel size scaled to points at 96 dpi** (`rect_pt = px *
0.75`). Recognition then renders that page at 300 dpi, i.e. `px * 300/96 = px * 3.125`. So
an image is **upscaled 3.125x** on the recognition path, where a PDF of the same visual
page is not. This is why the envelope is really sized around images, and specifically
around the phone photo, which is the single most common image Lyra ingests.

## Measurement

No binary corpus is committed to the repo (the eval corpora under `scripts/eval_corpora/`
are text cases), so the corpus here is the representative document **classes** Lyra targets,
measured through the real page-rectangle each would produce. The measurement is reproducible
as `backend/tests/test_render.py`:

- `test_the_envelope_accepts_the_representative_corpus`
- `test_the_envelope_refuses_pathological_material`
- `test_the_common_phone_photo_has_headroom_and_larger_ones_sit_near_the_edge`
- `test_the_raster_bound_is_exact_at_its_edges`

Raster pixel counts at the two full-page resolutions Lyra renders (reading 144 dpi,
recognition 300 dpi). Recognition (300) is the binding case.

### PDF pages (page points -> raster px)

| Class | Points | px @144 | px @300 (recognition) | Area @300 | Verdict |
| --- | --- | --- | --- | --- | --- |
| US Letter | 612x792 | 1224x1584 | 2550x3300 | 8.4 MP | accept |
| A4 | 595x842 | 1190x1684 | 2479x3508 | 8.7 MP | accept |
| A3 | 842x1191 | 1684x2382 | 3508x4962 | 17.4 MP | accept |
| Tabloid/Ledger | 792x1224 | 1584x2448 | 3300x5100 | 16.8 MP | accept |
| 16:9 lecture slide | 960x540 | 1920x1080 | 4000x2250 | 9.0 MP | accept |
| A2 | 1191x1684 | 2382x3368 | 4962x7016 | 34.8 MP | accept |
| A1 | 1684x2384 | 3368x4768 | 7016x9933 | 69.7 MP | accept |
| A0 poster | 2384x3370 | 4768x6740 | 9933x14041 | 139.5 MP | accept |

### Images (decoded px -> raster px; 3.125x at 300 dpi)

| Class | Decoded px | px @144 | px @300 (recognition) | Area @300 | Verdict |
| --- | --- | --- | --- | --- | --- |
| 12MP phone photo | 4032x3024 | 6048x4536 | 12600x9450 | 119.1 MP | accept (design point) |
| 16MP phone photo | 5312x2988 | 7968x4482 | 16600x9337 | 155.0 MP | accept (near edge) |
| 300-dpi Letter scan | 2550x3300 | 3825x4950 | 7968x10312 | 82.2 MP | accept |
| 1080p screenshot | 1920x1080 | 2880x1620 | 6000x3375 | 20.2 MP | accept |

### Refused

| Class | Geometry | px @300 | Area @300 | Caught by |
| --- | --- | --- | --- | --- |
| 24MP photo | 6000x4000 px | 18750x12500 | 234.4 MP | area |
| 5000pt page | 5000x5000 pt | 20833x20833 | 434.0 MP | area |
| Extreme MediaBox | 14400x14400 pt | 60000x60000 | 3600 MP | per-side |

## Why 175 MP and 30000 px, and not the previous 100 MP

- **Max pixel area encountered (accepted):** 155 MP (16MP phone photo @300). **Max single
  dimension encountered:** ~16600 px (same). A0 posters reach 139.5 MP / 14041 px.
- **The previous 100 MP ceiling refused the standard 12MP phone photo** (119.1 MP @300) -
  a core Lyra input - so keeping it would reject legitimate everyday material. That is the
  measurement that moved the number.
- **175 MP** admits the 12MP design point with ~32% headroom, admits 16MP phones (155 MP,
  ~11% headroom) and A0-scale PDFs (139.5 MP), while a 3 B/px pixmap stays ~525 MB - a few
  hundred MB, not the multi-GB spike the bound exists to stop. It still refuses a 24MP photo
  (234 MP), a 5000 pt page (434 MP), and the 14400 pt worst case (3.6 billion).
- **30000 px per side** is generous for every legitimate case above (largest single side
  ~16600 px) and exists only to catch a needle-thin page whose area is modest but whose one
  dimension would still allocate a degenerate buffer (e.g. a 30001x100 strip).

**Margin summary:** largest accepted raster 155 MP vs. 175 MP ceiling (~13% under);
design-point 12MP photo 119 MP (~32% under). First refused ordinary-looking input is a 24MP
photo at 234 MP.

## Note on cached pages

A cached page/figure is served without re-checking the envelope. That is safe even if the
envelope later tightens: the file exists only because a prior render already allocated its
pixmap and completed the atomic write, so the allocation this bound guards has already
happened and cannot recur by reading bytes back. A refused render never writes a file, and
the write is atomic (partial then rename), so `exists()` never returns a partial artifact.
Caches are therefore left in place across a limit change.

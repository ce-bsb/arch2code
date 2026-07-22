# `app.ingest` — universal diagram ingestion

Turns *whatever the user drops on the page* into one of two things, always the
same two: a **structured graph** (`structured.json`) and/or a list of **normalized
PNGs** (`vision/NNN.png`). Everything downstream keeps seeing exactly what it sees
today.

```python
from app.ingest import inspect_file, normalize

report = inspect_file("whiteboard.heic")          # cheap, read-only, no writes
report.summary.format_id                          # 'heic'
report.summary.vision_required                    # True  -> this will cost tokens
report.summary.requires_page_selection            # False -> nothing to ask

result = normalize("architecture.pdf", out_dir, pages=[3])
result.structured_path                            # out_dir/structured.json
result.pages[0].path                              # out_dir/vision/003.png
```

## Three rules

**1. Content decides the type, never the extension.** `mimetypes.guess_type` —
what this repo used before — is pure string matching on the suffix, so a PDF
renamed `sketch.png` was accepted as an image and handed to a decoder that could
not read it. Detection is a three-level cascade and a file whose bytes contradict
its name is **refused**.

| Level | Method | Separates |
|---|---|---|
| 1 | binary signature (`filetype`, 261 bytes) + SQLite/OLE2/JET/PNG-`tEXt` | PDF, PNG, JPEG, WebP, GIF, TIFF, BMP, HEIC, `.qea`, legacy Office, draw.io-in-PNG |
| 2 | ZIP central directory → `[Content_Types].xml` / `mimetype` / `canvas.json` | `.vsdx` vs `.pptx` vs `.docx` vs `.xlsx` vs `.odg` vs Miro `.rtb` — all of which start `50 4B 03 04` |
| 3 | first 8 KiB decoded as text | `.drawio`, `.svg`, `.bpmn`, `.archimate`, `.puml`, `.mmd`, `.mdj` — for all of which level 1 returns `None` |

The extension is used for exactly two things: breaking a tie between formats that
share a level-3 signature (`.drawio` vs `.xml`), and naming the contradiction when
one is found. One softening, deliberate and documented: two *raster* formats
disagreeing (a PNG named `.jpg`) is a note, not a refusal — both decode through
the same Pillow path, and re-saving a screenshot with the wrong suffix is an
everyday accident rather than an attack. Cross-family disagreement is always a
refusal.

**2. A structured source always beats vision.** Vision costs tokens and imports
hallucination; a `.drawio`, `.vsdx` or `.bpmn` states its nodes and edges with ids
that can be cited. `IngestSummary.vision_required` is computed from the adapter's
declared capability, not from habit.

**3. Every format we cannot read gets a way out.** Each refusal in `formats.py`
carries a `remedy` — the two clicks that turn a `.vsd` into a `.vsdx`, a `.pptx`
slide into a PDF, an Astah model into an image. A test fails the build if a
refusal ships without one.

## Format matrix

| Format | Detected at | Yields | Library | System dependency |
|---|---|---|---|---|
| PNG / JPEG / WebP / GIF / BMP | signature | raster | Pillow | none |
| TIFF (multi-page) | signature | raster, one page per frame | Pillow | none |
| **HEIC / HEIF** (iPhone) | signature | raster → PNG | pillow-heif | **none** (bundled libheif) |
| **PNG exported from draw.io** | signature (`tEXt`/`zTXt` key `mxfile`) | **structure + raster** | stdlib | none |
| PDF | signature | text + geometry, and raster per page | pypdfium2 | none |
| `.drawio` / `.xml` | text | structure (nodes, edges, geometry, every tab) | stdlib | none |
| SVG | text | structure (labels + positions; draw.io's embedded `content` gives the full graph) | stdlib | none |
| PlantUML | text | structure | none — it is text | none |
| Mermaid | text | structure | none — it is text | none |
| BPMN 2.0 | text | structure (+ `dc:Bounds` layout) | stdlib | none |
| ArchiMate (both dialects) | text | structure | stdlib | none |
| StarUML `.mdj` | text | structure | stdlib | none |
| Sparx EA 16+ `.qea` | signature (SQLite) | structure (`t_object` + `t_connector`) | stdlib `sqlite3` | none |
| Visio `.vsdx` | container | structure (shapes + glued connectors) | vsdx | none |
| Markdown / JSON / YAML / text | text | passed through verbatim | stdlib | none |

### Refused, with instructions

`.pptx` · `.docx` · `.xlsx` · `.vsd`/`.vdx` (binary Visio) · `.odg`/`.odp` ·
Miro `.rtb` · Sparx `.eap`/`.eapx` · Astah `.asta` · unknown ZIP · unknown XML ·
anything unrecognized.

`GET /api/uploads/formats` returns the whole table, refusals and remedies
included, so the UI can tell a user how to convert their file **before** they
upload it.

## What is deliberately *not* installed

| Binary | Would enable | Why not |
|---|---|---|
| `libmagic1` | `python-magic` | `filetype` does the same job in pure Python |
| `poppler-utils` | `pdf2image` | pypdfium2 renders in-process, faster |
| PyMuPDF (not a binary, a licence) | `get_drawings()` vector extraction | AGPL-or-commercial, and this **is** a network service. pypdfium2 is Apache-2.0/BSD-3 |
| `graphviz`, JRE | rendering PlantUML | PlantUML is read as text; rendering it to look at it with a vision model would be paying tokens to un-read something already written down |
| LibreOffice | `.vsd`, SmartArt, EMF/WMF, `.odg` | ~400 MB, needs a font package or it silently renders empty rectangles where text should be, and is not concurrency-safe with a shared profile. Those formats are refused with a conversion instruction instead |
| `mdbtools` | Sparx `.eap` | one `File > Save Project As` inside EA produces the `.qea` we read natively |

## Multi-page: the cost question

A 30-page PDF is 30 vision calls, and one trivial stage in this pipeline already
measured 37,154 tokens. So ingest **never picks a page**. `inspect_file` reports
every page with its character count, whether it is vector or raster, and whether
it looks like a diagram; `IngestSummary.requires_page_selection` travels up to the
upload response so the UI asks a human. `MAX_PAGES_PER_CALL` (20) is the hard stop
behind that.

The question is only asked where the answer costs something: a `.drawio` with
three tabs is parsed completely, for free, before anyone could have answered — so
`requires_page_selection` is false there, and a note says all tabs were read.

## Rasterization

EXIF transpose → flatten transparency onto white → longest edge to `MAX_EDGE`
(1568) with LANCZOS → PNG. PDF pages render at 200 DPI first and are *then*
resampled down: supersampling preserves small label text far better than
rendering straight at the target size.

`MAX_EDGE = 1568` is inherited from `capture_diagram.py` and is an Anthropic
constant, **not** a documented IBM limit — no watsonx.ai resolution or size limit
was found. Treat it as a tunable, and measure tokens per image per model before
trusting it.

## Layout of the output

```
<out_dir>/
  structured.json   StructuredGraph: nodes, edges, evidence, warnings
  ingest.json       the NormalizeResult, so a run is reproducible from disk
  vision/001.png    normalized pages, only when vision is actually required
```

`mcp/arch_vision/server.py::ALLOWED_IMAGE_TYPES` stays `{png, jpeg, webp, gif}`.
That guard is correct and must not be widened — HEIC must never reach the watsonx
endpoint. This package is what makes it sufficient.

## Testing

`webapp/tests/test_ingest.py` runs every adapter against a file
`webapp/tests/ingest_samples.py` **generates** — a real deflate-compressed
`.drawio`, a real PNG `tEXt` chunk, a real PDF with a real xref table, a real
HEIC, a real VSDX with a real `<Connect FromCell="BeginX">` pair. Nothing binary
is committed. A parser tested against a hand-written dict passes forever and still
fails on the first real export, because the interesting part of every one of these
formats is the encoding.

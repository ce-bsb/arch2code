"""Type detection by content, in three levels — never by extension alone.

The bug this module closes
--------------------------
Before it, ``routing.py`` decided everything from ``path.suffix``. Rename
``report.pdf`` to ``sketch.png`` and the app happily called it a screenshot,
handed it to ``capture_diagram.py``, and the failure surfaced several frames away
as a Pillow ``UnidentifiedImageError``. Worse, a suffix is attacker-controlled
text: it is the last thing that should pick which parser opens a file.

The cascade, exactly as specified
---------------------------------
**Level 1 — binary signature.** ``filetype`` (pure Python, reads the first 261
bytes, no ``libmagic1`` apt package) plus four signatures it does not carry that
matter here: SQLite (Sparx EA 16+), OLE2 (legacy Visio/Office), MS Access JET
(Sparx EA classic) and the draw.io XML that hides inside a draw.io-exported PNG.

**Level 2 — ZIP container.** ``.vsdx``, ``.pptx``, ``.docx``, ``.xlsx``, ``.odg``
and Miro's ``.rtb`` are all ZIP and all start with ``50 4B 03 04``. Signatures
cannot separate them; the ``[Content_Types].xml`` part inside can, and does.

**Level 3 — text.** ``.drawio``, ``.svg``, ``.bpmn``, ``.archimate``, ``.puml``,
``.mmd`` and ``.mdj`` are text, for which ``filetype`` returns ``None``. Sniffed
from the first 8 KiB.

Then the tie-break and the contradiction check
----------------------------------------------
The extension only ever breaks a tie (``.drawio`` vs ``.xml``, both of which are
XML text). When the content is decisive and the suffix says something else, the
upload is **refused** — that is the attack surface the briefing names. One
deliberate softening, documented rather than hidden: two *raster image* formats
disagreeing (a PNG named ``.jpg``) is downgraded to a note, because both go
through the same Pillow decode and re-saving a screenshot with the wrong suffix
is an everyday accident, not an attack. Cross-family disagreement — a PDF named
``.png``, a ZIP named ``.svg`` — is always a refusal.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import UnsupportedFormat
from .formats import UNKNOWN, FormatSpec, extension_claims, spec_for
from .models import DetectionLevel

__all__ = ["Detection", "detect", "detect_path", "sniff"]

#: How much of a text file is enough to recognize it. draw.io writes a long
#: ``<?xml ... ?>`` plus a ``<mxfile ...>`` attribute list before the payload, so
#: 512 bytes (the briefing's sketch) is not always enough. 8 KiB always is.
_TEXT_HEAD = 8192

#: filetype's mime -> our format id. Anything filetype recognizes that is not in
#: here is a binary we have no adapter for, and it becomes a refusal with the
#: detected mime quoted back at the user.
_SIGNATURE_MIME: dict[str, str] = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
    "image/x-ms-bmp": "bmp",
    "image/heic": "heic",
    "image/heif": "heic",
}

_ZIP_SIG = b"PK\x03\x04"
_SQLITE_SIG = b"SQLite format 3\x00"
_OLE2_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_JET_SIGS = (b"Standard Jet DB", b"Standard ACE DB")

_MERMAID_RE = re.compile(
    r"^(graph\s+(TB|TD|BT|RL|LR)|flowchart\s+(TB|TD|BT|RL|LR)|sequenceDiagram|"
    r"classDiagram(-v2)?|erDiagram|stateDiagram(-v2)?|journey|gitGraph|mindmap|"
    r"timeline|C4Context|C4Container|C4Component|C4Dynamic|requirementDiagram|"
    r"quadrantChart|block-beta)\b",
    re.MULTILINE,
)
_PLANTUML_RE = re.compile(
    r"^@start(uml|mindmap|salt|gantt|wbs|json|yaml|ebnf|chen)\b", re.MULTILINE
)


@dataclass
class Detection:
    """What the cascade concluded about one file."""

    spec: FormatSpec
    level: DetectionLevel
    extension: str = ""
    extension_agrees: bool = True
    #: Human-readable trace of how the verdict was reached, surfaced in the UI.
    notes: list[str] = field(default_factory=list)
    #: Set when level 2 opened a container: what was found inside.
    container_hint: str | None = None

    @property
    def format_id(self) -> str:
        return self.spec.id

    @property
    def mime(self) -> str:
        return self.spec.mime

    @property
    def capability(self) -> str:
        return self.spec.capability


# --------------------------------------------------------------------------- #
# Level 1 — binary signature
# --------------------------------------------------------------------------- #
def _guess_signature(data: bytes) -> tuple[str | None, str | None]:
    """Return ``(format_id, unhandled_mime)``.

    ``unhandled_mime`` is set when ``filetype`` recognized the bytes but we have
    no adapter — that is a much better refusal message than "unknown file", so it
    is carried out of here rather than discarded.
    """
    try:
        import filetype  # pure python, no system library
    except ImportError:  # pragma: no cover - listed in requirements.txt
        return None, None

    kind = filetype.guess(data[:4096])
    if kind is None:
        return None, None
    mime = (kind.mime or "").lower()
    fmt = _SIGNATURE_MIME.get(mime)
    if fmt:
        return fmt, None
    if mime in {"application/zip", "application/x-zip-compressed"}:
        return None, None  # level 2 owns ZIP
    return None, mime


def _custom_signature(data: bytes, ext: str) -> Detection | None:
    """Signatures ``filetype`` does not carry but that decide real formats here."""
    if data.startswith(_SQLITE_SIG):
        if ext in {".qea", ".qeax"}:
            return Detection(spec_for("sparx-qea"), "signature", ext)
        return Detection(
            FormatSpec(
                id="sqlite-unknown",
                label="SQLite database",
                family="model",
                capability="refuse",
                mime="application/vnd.sqlite3",
                remedy=(
                    "These bytes are a SQLite database, but the suffix is not .qea/.qeax "
                    "so it is not a Sparx Enterprise Architect 16+ repository. If it is "
                    "one, rename it to .qea and upload again; otherwise export the "
                    "diagram itself as .svg, .pdf or an image."
                ),
            ),
            "signature",
            ext,
        )
    if data[4:19] in _JET_SIGS or data[4:19].startswith(b"Standard "):
        return Detection(spec_for("sparx-eap"), "signature", ext)
    if data.startswith(_OLE2_SIG):
        if ext in {".vsd", ".vdx"}:
            return Detection(spec_for("visio-legacy"), "signature", ext)
        return Detection(
            FormatSpec(
                id="ole2-legacy",
                label="Legacy Microsoft Office binary (pre-2007)",
                family="office",
                capability="refuse",
                mime="application/x-ole-storage",
                remedy=(
                    "This is a pre-2007 binary Office file. Open it and re-save in the "
                    "modern format (.vsdx for Visio, .pptx, .docx) or export the "
                    "diagram as PDF, SVG or PNG, then upload that."
                ),
            ),
            "signature",
            ext,
        )
    return None


def _png_embedded_drawio(data: bytes) -> str | None:
    """Return the draw.io XML embedded in a draw.io-exported PNG, if any.

    draw.io writes the source diagram into a PNG ``tEXt``/``zTXt`` chunk keyed
    ``mxfile``. Nobody in this repo looked for it, so every draw.io PNG was paying
    for a vision call to re-read a graph that was sitting in the file already.
    This is the cheapest structural win available: a stdlib chunk walk.
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    import zlib

    pos = 8
    end = len(data)
    while pos + 8 <= end:
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if len(body) < length:
            return None
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            key, _, rest = body.partition(b"\x00")
            if key.lower() in (b"mxfile", b"mxgraphmodel"):
                try:
                    if ctype == b"tEXt":
                        payload = rest
                    elif ctype == b"zTXt":
                        payload = zlib.decompress(rest[1:])
                    else:  # iTXt: flag, method, lang\0, translated\0, text
                        flag = rest[0:1]
                        tail = rest[2:].split(b"\x00", 2)[-1]
                        payload = zlib.decompress(tail) if flag == b"\x01" else tail
                    text = payload.decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 - a broken chunk is not a broken PNG
                    return None
                from urllib.parse import unquote

                text = unquote(text)
                return text if "<mxfile" in text or "<mxGraphModel" in text else None
        if ctype == b"IDAT":
            # Metadata chunks precede the pixel data; past here there is nothing
            # to find and a 20 MB PNG would be walked for no reason.
            return None
        pos += 12 + length  # length + type + body + crc
    return None


# --------------------------------------------------------------------------- #
# Level 2 — ZIP container
# --------------------------------------------------------------------------- #
def _guess_container(data: bytes, ext: str) -> Detection | None:
    if not data.startswith(_ZIP_SIG):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            content_types = ""
            if "[Content_Types].xml" in names:
                content_types = zf.read("[Content_Types].xml").decode("utf-8", "replace")
            mimetype = ""
            if "mimetype" in names:
                mimetype = zf.read("mimetype").decode("utf-8", "replace").strip()
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        raise UnsupportedFormat(
            "ingest_zip_unreadable",
            "That archive could not be opened",
            f"The file starts with a ZIP signature but reading it failed: {exc}",
            remedy=(
                "The upload is probably truncated. Re-export or re-download it and "
                "check the file size is what you expect, then upload again."
            ),
            status=422,
        ) from exc

    lowered = content_types.lower()
    for needle, fmt in (
        ("ms-visio", "vsdx"),
        ("presentationml.presentation", "pptx"),
        ("wordprocessingml.document", "docx"),
        ("spreadsheetml.sheet", "xlsx"),
    ):
        if needle in lowered:
            return Detection(
                spec_for(fmt), "container", ext, container_hint="[Content_Types].xml"
            )
    if mimetype.startswith("application/vnd.oasis.opendocument"):
        return Detection(
            spec_for("opendocument-graphics"), "container", ext,
            container_hint=f"mimetype={mimetype}",
        )
    if "canvas.json" in names:
        return Detection(spec_for("miro-rtb"), "container", ext, container_hint="canvas.json")
    return Detection(spec_for("zip-unknown"), "container", ext,
                     container_hint=f"{len(names)} entries, no known manifest")


# --------------------------------------------------------------------------- #
# Level 3 — text
# --------------------------------------------------------------------------- #
def _guess_text(data: bytes, ext: str) -> Detection | None:
    head_bytes = data[:_TEXT_HEAD]
    if b"\x00" in head_bytes:
        return None  # NUL byte: this is binary, not text we failed to recognize
    head = head_bytes.decode("utf-8", "replace").lstrip("﻿ \t\r\n")
    if not head.strip():
        return None
    low = head.lower()

    if "<mxfile" in low or "<mxgraphmodel" in low:
        return Detection(spec_for("drawio"), "text", ext)
    if "archimate" in low and ("<" in low):
        return Detection(spec_for("archimate"), "text", ext)
    if "bpmn" in low and head.lstrip().startswith("<"):
        return Detection(spec_for("bpmn"), "text", ext)
    if "<svg" in low:
        return Detection(spec_for("svg"), "text", ext)
    if _PLANTUML_RE.search(head):
        return Detection(spec_for("plantuml"), "text", ext)
    if _MERMAID_RE.search(head.lstrip()):
        return Detection(spec_for("mermaid"), "text", ext)
    if head.lstrip().startswith("{") and '"_type"' in head:
        return Detection(spec_for("staruml"), "text", ext)

    if head.lstrip().startswith("<?xml") or head.lstrip().startswith("<"):
        return Detection(
            FormatSpec(
                id="xml-unknown",
                label="XML document (no known diagram dialect)",
                family="unknown",
                capability="refuse",
                mime="application/xml",
                remedy=(
                    "This is XML, but its root element matches no diagram dialect read "
                    "here (draw.io mxfile, BPMN 2.0, ArchiMate, SVG). If it came from a "
                    "modelling tool, export the diagram as .svg, .pdf or an image "
                    "instead; if it is a draw.io file, open it in diagrams.net and use "
                    "File > Save as > .drawio."
                ),
            ),
            "text",
            ext,
        )

    # Plain text with no diagram grammar. Only the prose extensions may claim it —
    # a random .bin full of ASCII is not an architecture description.
    if ext in spec_for("prose").extensions:
        notes = []
        if "```mermaid" in low:
            notes.append(
                "This document contains a ```mermaid fenced block. Extracting that "
                "block into a .mmd file and uploading it gives an exact graph."
            )
        return Detection(spec_for("prose"), "text", ext, notes=notes)
    return None


# --------------------------------------------------------------------------- #
# The cascade
# --------------------------------------------------------------------------- #
#: Formats that are all "a bitmap Pillow opens". Disagreement inside this set is a
#: mislabelled screenshot, not an attack — see the module docstring.
_RASTER_PEERS = frozenset({"png", "jpeg", "webp", "gif", "bmp", "tiff", "heic", "drawio-png"})


def detect(data: bytes, filename: str = "") -> Detection:
    """Run the three-level cascade over ``data``.

    :param data: the file's bytes. The whole file, not a prefix: level 2 has to
        open the ZIP central directory, which lives at the *end*.
    :param filename: used only for the suffix, and only as a tie-breaker.
    :raises UnsupportedFormat: when the content contradicts the suffix.
    """
    if not data:
        raise UnsupportedFormat(
            "ingest_empty_file",
            "That file is empty",
            "Zero bytes were received, so there is nothing to detect.",
            remedy="Re-export the diagram and check the file size before uploading.",
            status=422,
        )

    ext = Path(filename or "").suffix.lower()
    detection = _run_cascade(data, ext)
    _check_extension_agreement(detection, ext)
    return detection


def _run_cascade(data: bytes, ext: str) -> Detection:
    custom = _custom_signature(data, ext)
    if custom is not None:
        return custom

    fmt, unhandled_mime = _guess_signature(data)
    if fmt == "png":
        embedded = _png_embedded_drawio(data)
        if embedded:
            return Detection(
                spec_for("drawio-png"),
                "signature",
                ext,
                notes=[
                    "This PNG was exported from draw.io with the diagram embedded. "
                    "The graph is read from that XML exactly, so no vision tokens "
                    "are spent."
                ],
            )
    if fmt:
        return Detection(spec_for(fmt), "signature", ext)

    container = _guess_container(data, ext)
    if container is not None:
        return container

    text = _guess_text(data, ext)
    if text is not None:
        return text

    if unhandled_mime:
        return Detection(
            FormatSpec(
                id=f"binary-{unhandled_mime.replace('/', '-')}",
                label=f"Binary file ({unhandled_mime})",
                family="unknown",
                capability="refuse",
                mime=unhandled_mime,
                remedy=(
                    f"These bytes are {unhandled_mime}, which is not a diagram format "
                    "read here. Export the drawing as .png, .jpg, .pdf, .svg, .drawio, "
                    ".vsdx, .bpmn, .puml or .mmd and upload that."
                ),
            ),
            "signature",
            ext,
        )

    # Nothing matched by content. The suffix gets one last word — but only to name
    # the refusal, never to unlock an adapter it did not earn.
    return Detection(UNKNOWN, "none", ext)


def _check_extension_agreement(detection: Detection, ext: str) -> None:
    """Refuse a file whose bytes contradict its suffix. Mutates ``detection``."""
    detection.extension = ext
    if detection.spec.capability == "refuse":
        return  # already refused; a suffix quarrel adds nothing
    claims = extension_claims(ext)
    if not ext or not claims:
        if ext:
            detection.notes.append(
                f"The suffix '{ext}' is not one this app knows; the file was "
                f"identified as {detection.spec.label} from its contents."
            )
        return
    if detection.format_id in claims:
        return

    # Text-level detection refines rather than contradicts: a .xml holding an
    # <mxfile> is a draw.io file, and saying so is an upgrade, not a conflict.
    if detection.level == "text":
        detection.notes.append(
            f"'{ext}' was read as {detection.spec.label} from its contents."
        )
        return

    if detection.format_id in _RASTER_PEERS and any(c in _RASTER_PEERS for c in claims):
        detection.extension_agrees = False
        detection.notes.append(
            f"The suffix says '{ext}' but the bytes are {detection.spec.label}. Both "
            f"are bitmaps and decode identically, so the real type is used — but the "
            f"file is mislabelled and worth re-exporting."
        )
        return

    claimed = ", ".join(spec_for(c).label for c in claims)
    raise UnsupportedFormat(
        "ingest_extension_mismatch",
        "That file is not what its name says it is",
        f"The suffix '{ext}' claims {claimed}, but the first bytes are "
        f"{detection.spec.label} ({detection.mime}).",
        remedy=(
            f"Rename the file to its real type (a {detection.spec.label} should end in "
            f"{detection.spec.extensions[0] if detection.spec.extensions else 'its own suffix'}) "
            "and upload it again. A file is opened by what it contains, never by what "
            "it is called — a renamed file would otherwise be handed to the wrong parser."
        ),
        detected=detection.format_id,
        extension=ext,
    )


def detect_path(path: Path | str) -> Detection:
    """:func:`detect` for a file already on disk."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise UnsupportedFormat(
            "ingest_unreadable_file",
            "That file could not be read",
            f"{p}: {exc}",
            remedy="Check the path exists and is readable, then try again.",
            status=422,
        ) from exc
    return detect(data, p.name)


def sniff(path: Path | str) -> str:
    """The format id only — the short form used in logs and tests."""
    return detect_path(path).format_id

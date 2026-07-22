"""Turning anything with pixels into the one PNG shape the vision path accepts.

The rules, and why each exists:

* **EXIF transpose first.** A phone photo arrives sideways. A vision model reads a
  diagram rotated 90 degrees and answers confidently with garbage — the worst
  possible failure, because it looks like success.
* **HEIC needs an opener registered.** ``routing.py``, ``capture_diagram.py`` and
  the upload endpoint all advertised ``.heic`` while ``requirements.txt`` had no
  decoder, so every iPhone photo — the single most likely real-world input for a
  whiteboard — raised ``UnidentifiedImageError``. ``register_heif_opener()`` is
  called exactly once, here, before any ``Image.open``.
* **Longest edge to 1568 px with LANCZOS.** Marked ``[NV]`` in the briefing: 1568
  is an Anthropic constant and no equivalent IBM limit was found for watsonx
  vision models. It is kept because it is the inherited behaviour of
  ``capture_diagram.py`` and changing it would silently alter Mode A's output; it
  is a tunable, not a contract, and ``MAX_EDGE`` is the one place to change it.
* **Supersample, then downscale.** Rendering a PDF at 200 DPI and resampling down
  to 1568 px is visibly better than rendering straight at the target size: small
  label text survives. It is the same reason ``capture_diagram.py`` resizes rather
  than asking the decoder for a thumbnail.
* **PNG out, always.** ``mcp/arch_vision/server.py`` accepts png/jpeg/webp/gif and
  nothing else. That guard is correct and must not be widened; the normalizer is
  what makes it sufficient.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .errors import CorruptSource, MissingDependency

__all__ = ["MAX_EDGE", "open_image", "normalize_image", "save_normalized"]

#: Longest edge of any PNG that reaches the model. See the module docstring: this
#: is an inherited heuristic, not a documented IBM limit.
MAX_EDGE = 1568

_heif_lock = threading.Lock()
_heif_state: dict[str, Any] = {"registered": False, "error": None}


def _ensure_heif() -> None:
    """Register the HEIF opener once per process, and remember why if we cannot."""
    if _heif_state["registered"]:
        return
    with _heif_lock:
        if _heif_state["registered"]:
            return
        try:
            from pillow_heif import register_heif_opener
        except ImportError as exc:
            _heif_state["error"] = str(exc)
            raise MissingDependency(
                "ingest_heif_decoder_missing",
                "HEIC/HEIF photos cannot be decoded on this install",
                "pillow-heif is not installed, and Pillow alone cannot read HEIC.",
                remedy=(
                    "Install it into the interpreter named by ARCH2CODE_PYTHON: "
                    "`<python> -m pip install pillow-heif`. It ships manylinux/macOS "
                    "wheels with libheif bundled, so no apt-get or brew step is needed. "
                    "Until then, on the iPhone: Settings > Camera > Formats > Most "
                    "Compatible makes the camera write JPEG instead."
                ),
            ) from exc
        register_heif_opener()
        _heif_state["registered"] = True


def open_image(path: Path):
    """``PIL.Image.open`` with HEIF support and an actionable failure.

    The caller is responsible for closing the returned image (or using it as a
    context manager), exactly like ``Image.open``.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow is a hard requirement
        raise MissingDependency(
            "ingest_pillow_missing",
            "Pillow is not installed",
            "The image pipeline cannot run without Pillow.",
            remedy=(
                "`<ARCH2CODE_PYTHON> -m pip install -r webapp/requirements.txt`. The "
                "system python3 on macOS is 3.9.6 and has none of the dependencies."
            ),
        ) from exc

    suffix = path.suffix.lower()
    if suffix in {".heic", ".heif", ".hif"}:
        _ensure_heif()
    try:
        return Image.open(path)
    except Exception as exc:  # noqa: BLE001 - Pillow raises a wide family here
        # A HEIC saved with a .jpg name is a real case; retry once with the opener.
        if not _heif_state["registered"]:
            try:
                _ensure_heif()
                return Image.open(path)
            except MissingDependency:
                pass
            except Exception:  # noqa: BLE001
                pass
        raise CorruptSource(
            "ingest_image_undecodable",
            "That image could not be decoded",
            f"{path.name}: {type(exc).__name__}: {exc}",
            remedy=(
                "The file is truncated or is not really an image. Open it in a viewer "
                "to confirm, then re-export it as PNG or JPEG and upload again."
            ),
        ) from exc


def normalize_image(image, *, max_edge: int = MAX_EDGE) -> tuple[Any, dict[str, Any]]:
    """EXIF-transpose, flatten to RGB and downscale. Returns ``(image, meta)``.

    ``meta`` records what happened so the manifest can explain a bbox that does not
    line up with the original file's pixel coordinates.
    """
    from PIL import Image, ImageOps

    original = {
        "width": image.width,
        "height": image.height,
        "mode": image.mode,
        "format": image.format,
    }
    image = ImageOps.exif_transpose(image) or image
    rotated = (image.width, image.height) != (original["width"], original["height"])

    if image.mode in ("RGBA", "LA", "P"):
        # A transparent background renders black in most viewers and the model reads
        # black-on-black as an empty region. Composite onto white instead.
        image = image.convert("RGBA")
        canvas = Image.new("RGB", image.size, (255, 255, 255))
        canvas.paste(image, mask=image.split()[-1])
        image = canvas
    elif image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    scale = 1.0
    if max(image.size) > max_edge:
        scale = max_edge / max(image.size)
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS,
        )

    meta = {
        "original": original,
        "normalized": {"width": image.width, "height": image.height},
        "exif_rotation_applied": rotated,
        "scale": round(scale, 4),
    }
    return image, meta


def save_normalized(image, dest: Path, *, max_edge: int = MAX_EDGE) -> dict[str, Any]:
    """Normalize and write a PNG, returning the metadata dict."""
    image, meta = normalize_image(image, max_edge=max_edge)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest, "PNG", optimize=True)
    meta["path"] = str(dest)
    meta["bytes"] = dest.stat().st_size
    return meta

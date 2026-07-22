"""Bitmaps: PNG, JPEG, WebP, GIF, BMP, TIFF and HEIC/HEIF.

This is the only adapter with no structural path, and therefore the only one that
always spends vision tokens. Everything it does is aimed at making that spend
count: the photo is de-rotated before the model sees it, transparency is
flattened onto white, and the longest edge is capped so the provider is not
rescaling (and billing for) pixels nobody reads.

Two multi-frame cases are handled explicitly rather than by accident:

* **Multi-page TIFF** — a scanner produces one, and Pillow silently hands you only
  frame 0. Each frame becomes a page, exactly like a PDF, so the same "which page?"
  question reaches the user.
* **Animated GIF** — frame 0 is used and the fact is stated as a note. A silent
  drop of 40 frames is the kind of thing a reviewer discovers during the demo.

HEIC is here rather than in its own module because after ``register_heif_opener()``
it is a Pillow format like any other. The registration itself lives in
``ingest/raster.py`` and is the fix for the confirmed latent bug: three files
advertised ``.heic`` support while ``requirements.txt`` had no decoder.
"""

from __future__ import annotations

from pathlib import Path

from ..models import NormalizedPage, PageRef
from ..raster import MAX_EDGE, open_image, save_normalized
from .base import Adapter

__all__ = ["ImageAdapter"]


class ImageAdapter(Adapter):
    id = "image"
    label = "image"
    produces_raster = True

    def _frame_count(self, path: Path) -> int:
        with open_image(path) as im:
            return int(getattr(im, "n_frames", 1) or 1)

    def pages(self, path: Path) -> list[PageRef]:
        with open_image(path) as im:
            frames = int(getattr(im, "n_frames", 1) or 1)
            width, height = im.size
            fmt = (im.format or "").upper()
        if frames <= 1:
            return [
                PageRef(index=1, width=width, height=height, content="raster",
                        likely_diagram=True)
            ]
        name = "frame" if fmt == "GIF" else "page"
        return [
            PageRef(
                index=i,
                name=f"{name} {i}",
                width=width,
                height=height,
                content="raster",
                # Nothing distinguishes scanner pages without OCR, so the first is
                # suggested and the user is asked rather than told.
                likely_diagram=(i == 1),
            )
            for i in range(1, frames + 1)
        ]

    def rasterize(
        self, path: Path, out_dir: Path, pages: list[int] | None = None,
        *, max_edge: int = MAX_EDGE,
    ) -> list[NormalizedPage]:
        out_dir.mkdir(parents=True, exist_ok=True)
        results: list[NormalizedPage] = []
        with open_image(path) as im:
            frames = int(getattr(im, "n_frames", 1) or 1)
            wanted = sorted(set(pages)) if pages else list(range(1, frames + 1))
            for index in wanted:
                if index < 1 or index > frames:
                    continue
                if frames > 1:
                    im.seek(index - 1)
                dest = out_dir / f"{index:03d}.png"
                meta = save_normalized(im, dest, max_edge=max_edge)
                results.append(
                    NormalizedPage(
                        index=index,
                        path=str(dest),
                        width=meta["normalized"]["width"],
                        height=meta["normalized"]["height"],
                        scale=meta["scale"],
                        origin="convert",
                    )
                )
        return results

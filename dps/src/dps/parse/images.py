"""Photos and scans sent as PNG or JPEG (CTS integration, 23 Nov 2026: the bank app's upload accepts pictures).

A picture is checked (inspect_image), then turned into a one-page PDF (to_pdf) and read by the PDF path with OCR,
so a photo of a certificate or a statement is read exactly like a scanned PDF page. Nothing else changes: the page
model, the evidence (page 1, the OCR'd quote) and the review are the same as for a scan.
"""

from __future__ import annotations

import io

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
MAX_SIDE_PX = 12_000
MAX_PIXELS = 80_000_000  # a 12 000 x 6 600 scan; a decompression bomb is refused before it is decoded
PDF_DPI = 200


class ImageRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_image(data: bytes) -> bool:
    return data.startswith(PNG_MAGIC) or data.startswith(JPEG_MAGIC)


def _open(data: bytes):
    from PIL import Image

    if not is_image(data):
        raise ImageRefused("TYPE_SPOOFED", "The file's content is not a PNG or JPEG picture.")
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as ex:  # noqa: BLE001 - any decoder error is a corrupt picture
        raise ImageRefused("CORRUPT", "The picture cannot be read.") from ex
    if img.format not in ("PNG", "JPEG"):
        raise ImageRefused("TYPE_SPOOFED", "The file's content is not a PNG or JPEG picture.")
    width, height = img.size
    if max(width, height) > MAX_SIDE_PX or width * height > MAX_PIXELS:
        raise ImageRefused("TOO_LARGE", f"The picture is {width} x {height} pixels; the limit is {MAX_SIDE_PX} "
                                        "on a side.")
    return img


def inspect_image(data: bytes) -> None:
    """Raises ImageRefused (TYPE_SPOOFED, CORRUPT, TOO_LARGE) for a picture DPS will not read."""
    img = _open(data)
    try:
        img.verify()
    except Exception as ex:  # noqa: BLE001
        raise ImageRefused("CORRUPT", "The picture is damaged.") from ex


def to_pdf(data: bytes) -> bytes:
    """The picture as a one-page PDF (turned upright from its camera orientation, colours as RGB)."""
    from PIL import ImageOps

    img = _open(data)
    try:
        img.load()
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, "PDF", resolution=PDF_DPI)
    except ImageRefused:
        raise
    except Exception as ex:  # noqa: BLE001
        raise ImageRefused("CORRUPT", "The picture cannot be read.") from ex
    return out.getvalue()

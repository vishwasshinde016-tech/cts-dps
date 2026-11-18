"""Scanned pages the OCR cannot read: the vision route reads the page image (Miro AI function, "PDF may contain text
or image").

The worker sets ``page_images`` for the job it runs (a lazy renderer over the hash-checked file), so the skill code
never touches storage. Answers read from an image cannot be grounded against page text, so every value read here
goes to a person (status NEEDS_REVIEW, issue READ_FROM_IMAGE): the model helps the reviewer, it does not decide.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar

log = logging.getLogger(__name__)

MAX_IMAGES = 4
DPI = 150

page_images: ContextVar[Callable[[int], bytes | None] | None] = ContextVar("dps_page_images", default=None)


def renderer(read_bytes: Callable[[], bytes], dpi: int = DPI) -> Callable[[int], bytes | None]:
    """``page -> PNG bytes`` for a PDF (None for other formats or a page that does not exist). The file is read and
    opened once, on the first call."""
    state: dict = {}

    def render(no: int) -> bytes | None:
        if "pdf" not in state:
            data = read_bytes()
            if not data.startswith(b"%PDF"):
                state["pdf"] = None
            else:
                import pypdfium2 as pdfium

                state["pdf"] = pdfium.PdfDocument(data)
        pdf = state["pdf"]
        if pdf is None or no < 1 or no > len(pdf):
            return None
        image = pdf[no - 1].render(scale=dpi / 72).to_pil()
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()

    return render


def image_messages(text: str, images: list[tuple[int, bytes]]) -> list[dict]:
    parts: list[dict] = [{"type": "text", "text": text}]
    for no, png in images:
        parts.append({"type": "text", "text": f"Image of page {no}:"})
        parts.append({"type": "image_url",
                      "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}})
    return [{"role": "user", "content": parts}]


def read_pages(pack, gateway, page_nos: list[int], notes: list | None = None) -> list[dict] | None:
    """Proposals ({name, value, page, quote}) read from the images of ``page_nos``; None when there is no vision route,
    no renderer or the call failed (the reason goes into ``notes``)."""
    images = page_images.get()
    route = (pack.routes or {}).get("images") or "vision"
    available = getattr(gateway, "available", None)
    if gateway is None or images is None or not callable(available) or not available(route):
        return None
    from dps.agent.gateway import GatewayError
    from dps.ai_guard import DATA_NOT_INSTRUCTIONS
    from dps.skills.common import HEADER, issue

    pngs = []
    for no in page_nos[:MAX_IMAGES]:
        try:
            png = images(no)
        except Exception:  # noqa: BLE001 - a page that cannot be rendered is simply not sent
            log.warning("page %s could not be rendered for the vision route", no)
            png = None
        if png:
            pngs.append((no, png))
    if not pngs:
        return None
    allowed = {no for no, _ in pngs}
    text = "\n\n".join([HEADER.format(guard=DATA_NOT_INSTRUCTIONS), pack.prompt.strip(),
                        "Schema:\n" + json.dumps(pack.schema),
                        "The pages are scanned images: " + ", ".join(str(n) for n in sorted(allowed))
                        + ". For each item give the page number and copy the words exactly as printed."])

    def check(answer) -> str | None:
        for item in (answer or {}).get("items", []):
            if item.get("page") not in allowed:
                return f"page {item.get('page')} was not one of the images"
        return None

    try:
        answer = gateway.call_json(route, image_messages(text, pngs), pack.schema, check)
    except GatewayError as ex:
        if notes is not None:
            notes.append(issue(ex.code, f"vision: {str(ex)[:200]}"))
        return None
    return list(answer.get("items", []))

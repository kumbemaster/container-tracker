"""PDF handling for BL scanning with pypdfium2.

Renders PDF pages to JPEG bytes (through prepare_image) and reads the
text layer of each page. Never executes anything from the PDF: only
page rendering and text extraction are used.
"""

import math
from io import BytesIO

import pypdfium2 as pdfium

from .images import MAX_PIXELS, ImageError, prepare_image


class PdfError(ValueError):
    """The PDF could not be scanned."""


#: Shown when the PDF needs a password; safe to display to the agent.
PROTECTED_PDF_MESSAGE = (
    'This PDF is protected. Remove the password or upload an image.'
)

#: Refuse PDFs with more pages than this.
MAX_PDF_PAGES = 50

_PDF_MARKER = b'%PDF-'


def is_pdf(data):
    """True when the bytes look like a PDF by signature."""
    return isinstance(data, (bytes, bytearray)) and _PDF_MARKER in bytes(
        data[:1024]
    )


def _open(pdf_bytes):
    try:
        return pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as exc:
        message = str(exc).lower()
        if 'password' in message or _looks_encrypted(pdf_bytes):
            raise PdfError(PROTECTED_PDF_MESSAGE) from exc
        raise PdfError('The PDF could not be read.') from exc
    except Exception as exc:
        raise PdfError('The PDF could not be read.') from exc


def _looks_encrypted(pdf_bytes):
    head = bytes(pdf_bytes[:4096])
    tail = bytes(pdf_bytes[-4096:])
    return b'/Encrypt' in head or b'/Encrypt' in tail


def pdf_to_images(pdf_bytes, max_pages=5, max_side=2000):
    """Render the first pages of a PDF to JPEG bytes.

    Returns (images, pages_total, pages_read): a list of JPEG byte
    strings (at most max_pages, longest side max_side), the total page
    count and the number of pages rendered. Raises PdfError for an
    unreadable, password-protected, empty or oversized file. The
    document is always closed before returning.
    """
    document = _open(pdf_bytes)
    try:
        total = len(document)
        if total == 0:
            raise PdfError('The PDF has no pages to read.')
        if total > MAX_PDF_PAGES:
            raise PdfError(
                'The PDF has too many pages to scan (more than 50).'
            )
        count = min(total, max_pages)
        images = []
        for index in range(count):
            images.append(_render_page(document, index, max_side))
        return images, total, count
    finally:
        document.close()


def _render_page(document, index, max_side):
    page = document[index]
    try:
        width = page.get_width() or 1
        height = page.get_height() or 1
        scale = min(
            max_side / max(width, height),
            math.sqrt(MAX_PIXELS / (width * height)),
            4.0,
        )
        bitmap = page.render(scale=max(0.1, scale))
        try:
            pil_image = bitmap.to_pil()
        finally:
            bitmap.close()
        out = BytesIO()
        pil_image.save(out, format='PNG')
        png_bytes = out.getvalue()
    finally:
        page.close()
    try:
        return prepare_image(png_bytes, max_side=max_side)
    except ImageError as exc:
        raise PdfError('The PDF pages are too large to scan.') from exc


def _page_text(document, index, budget):
    if budget <= 0:
        return ''
    page = document[index]
    try:
        try:
            text_page = page.get_textpage()
        except Exception:
            return ''
        try:
            return (text_page.get_text_range() or '')[:budget]
        except Exception:
            return ''
        finally:
            try:
                text_page.close()
            except Exception:
                pass
    finally:
        page.close()


def pdf_text(pdf_bytes, max_pages=5, max_chars=6000):
    """Return the text layer of each page ('' when there is none).

    At most max_pages pages and max_chars characters in total. Raises
    PdfError for an unreadable or password-protected file. The document
    is always closed before returning.
    """
    document = _open(pdf_bytes)
    try:
        total = len(document)
        if total == 0:
            raise PdfError('The PDF has no pages to read.')
        if total > MAX_PDF_PAGES:
            raise PdfError(
                'The PDF has too many pages to scan (more than 50).'
            )
        texts = []
        remaining = max_chars
        for index in range(min(total, max_pages)):
            texts.append(_page_text(document, index, remaining))
            remaining = max(0, remaining - len(texts[-1]))
        return texts
    finally:
        document.close()

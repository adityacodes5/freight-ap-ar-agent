import base64
import io

import pdfplumber
from loguru import logger


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract all text from a PDF, page by page.
    Returns the raw text; empty string if the PDF is a scanned image."""
    pages = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        clean = [cell or "" for cell in row]
                        line = " | ".join(clean).strip()
                        if line and line not in text:
                            pages.append(line)
    except Exception as exc:
        logger.error("PDF extraction failed: {}", exc)

    result = "\n\n".join(pages)
    logger.debug("Extracted {} characters from PDF", len(result))
    return result


def iter_pdf_pages_base64(pdf_bytes: bytes, max_pages: int = 15, scale: float = 1.5):
    """Yield each PDF page as a base64 JPEG, one at a time (lazy).

    Lets the caller render page 1, check it, and only render page 2 if it still
    needs more — so a load number found early avoids rendering the rest. Used as
    fallback for scanned/image PDFs where text extraction yields nothing. Renders
    at `scale`x (default 1.5x ≈ 108 DPI), encodes JPEG, frees each page's buffers
    before the next, so memory stays low on the 512MB backend. The caller can pass
    a higher `scale` (e.g. 3.0 ≈ 216 DPI) to retry a low-quality/faxed scan that
    was illegible at the default resolution.
    Requires the 'pypdfium2' package (pip install pypdfium2)."""
    doc = None
    try:
        import pypdfium2 as pdfium  # optional dependency

        doc = pdfium.PdfDocument(pdf_bytes)
        n = min(len(doc), max_pages)
        for i in range(n):
            page = doc[i]
            bitmap = page.render(scale=scale)  # scale×72 DPI — readable, low memory
            pil_image = bitmap.to_pil()
            if pil_image.mode != "RGB":
                pil_image = pil_image.convert("RGB")  # JPEG needs RGB
            buf = io.BytesIO()
            pil_image.save(buf, format="JPEG", quality=80, optimize=True)
            data = base64.standard_b64encode(buf.getvalue()).decode()
            # Free this page's allocations before yielding / moving on
            buf.close()
            pil_image.close()
            bitmap.close()
            page.close()
            yield data
    except ImportError:
        logger.debug("pypdfium2 not installed — cannot render scanned PDF")
    except Exception as exc:
        logger.warning("PDF render failed: {}", exc)
    finally:
        if doc is not None:
            doc.close()


def pdf_first_page_as_base64(pdf_bytes: bytes) -> str | None:
    """Back-compat single-page helper — returns first page only, or None."""
    for img in iter_pdf_pages_base64(pdf_bytes, max_pages=1):
        return img
    return None
